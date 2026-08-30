import concurrent.futures
import datetime as dt
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
import pathlib
import re
import socket
import ssl
import struct
import tempfile
import urllib.parse

from .core import Context, OpsError, bounded, finding, run, run_json
from .registry import operation

def _state_base():
    configured = os.environ.get("XDG_STATE_HOME", "")
    candidate = pathlib.Path(os.path.expanduser(configured)) if configured else None
    if candidate is None or not candidate.is_absolute():
        candidate = pathlib.Path.home() / ".local" / "state"
    return candidate


STATE_HOME = _state_base() / "agent-ops"
KUBECONFIG_HOME = STATE_HOME / "kubeconfigs"
EKS_STATE = STATE_HOME / "eks-state.json"
STALE_SECONDS = 20 * 60 * 60
ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def _ensure_private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if path.is_symlink() or not path.is_dir() or info.st_uid != os.getuid():
        raise OpsError("unsafe_state_directory", "Managed state directory must be a real owner-controlled directory", 3)
    path.chmod(0o700)


def _target_id(args):
    target = args.get("target")
    if not isinstance(target, str) or not ID_RE.fullmatch(target):
        raise OpsError("invalid_target", "Target ID must match [a-z][a-z0-9_-]{0,63}", 3)
    return target


def _safe_value(value, label, max_length=253):
    if not isinstance(value, str) or not value or value.startswith("-") or len(value) > max_length or any(ord(char) < 32 for char in value):
        raise OpsError("invalid_target", f"{label} is invalid", 3)
    return value


def _kube_path(target):
    return KUBECONFIG_HOME / f"{target}.yaml"


def _load_state():
    try:
        value = json.loads(EKS_STATE.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_state(value):
    _ensure_private_dir(STATE_HOME)
    fd, name = tempfile.mkstemp(prefix="eks-state.", dir=STATE_HOME)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
        os.replace(name, EKS_STATE)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _update_state(target, entry):
    _ensure_private_dir(STATE_HOME)
    lock_path = STATE_HOME / ".eks-state.lock"
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = _load_state()
        state[target] = entry
        _save_state(state)


def eks_status_data(target):
    path = _kube_path(target)
    entry = _load_state().get(target, {})
    refreshed = entry.get("refreshed_at", 0)
    age = max(0, int(dt.datetime.now(dt.timezone.utc).timestamp() - refreshed)) if refreshed else None
    valid_file = path.is_file() and (path.stat().st_mode & 0o077) == 0
    return {
        "target": target,
        "available": valid_file,
        "stale": not valid_file or age is None or age >= STALE_SECONDS,
        "age_seconds": age,
        "aws_profile": entry.get("aws_profile"),
        "aws_region": entry.get("aws_region"),
        "eks_cluster": entry.get("eks_cluster"),
        "context": target if valid_file else None,
    }


@operation("eks.status", required=("target",), runbook=True, mutation="none")
def eks_status(ctx, args):
    target = _target_id(args)
    info = eks_status_data(target)
    status = "degraded" if info["stale"] else "healthy"
    findings = [finding("warning", "kubeconfig_stale", "Managed kubeconfig is absent or stale", target)] if info["stale"] else []
    return ctx.success(status=status, target={"id": target}, summary={"available": info["available"], "stale": info["stale"]}, findings=findings, data=info)


@operation("eks.refresh", required=("target", "aws_profile", "region", "cluster"), executables=("aws",), mutation="local_cache", timeout=60)
def eks_refresh(ctx, args):
    target = _target_id(args)
    profile = _safe_value(args.get("aws_profile"), "AWS profile")
    region = _safe_value(args.get("region"), "AWS region")
    cluster = _safe_value(args.get("cluster"), "EKS cluster")
    _ensure_private_dir(STATE_HOME)
    _ensure_private_dir(KUBECONFIG_HOME)
    lock_path = KUBECONFIG_HOME / f".{target}.lock"
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        fd, temp_name = tempfile.mkstemp(prefix=f".{target}.", suffix=".yaml", dir=KUBECONFIG_HOME)
        os.close(fd)
        os.chmod(temp_name, 0o600)
        try:
            run(ctx, ["aws", "--profile", profile, "--region", region, "eks", "update-kubeconfig", "--name", cluster, "--kubeconfig", temp_name, "--alias", target, "--user-alias", target], timeout=60)
            text = pathlib.Path(temp_name).read_text(errors="replace")
            aliases = re.findall(rf"(?m)^\s*-?\s*name:\s*{re.escape(target)}\s*$", text)
            if len(text) > 1_000_000 or len(aliases) < 3 or not re.search(rf"(?m)^current-context:\s*{re.escape(target)}\s*$", text):
                raise OpsError("invalid_kubeconfig", "Generated kubeconfig did not contain the expected target alias", 6)
            os.replace(temp_name, _kube_path(target))
            os.chmod(_kube_path(target), 0o600)
            _update_state(target, {"refreshed_at": int(dt.datetime.now(dt.timezone.utc).timestamp()), "aws_profile": profile, "aws_region": region, "eks_cluster": cluster})
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
    return ctx.success(target={"id": target, "aws_profile": profile, "aws_region": region, "eks_cluster": cluster}, summary={"refreshed": True, "context": target})


def ensure_target(ctx, args):
    target = _target_id(args)
    path = _kube_path(target)
    if not path.is_file():
        raise OpsError("target_unavailable", "Managed kubeconfig is absent; run eks refresh or use a multicluster runbook", 3)
    actual, _, _ = run(ctx, ["kubectl", "--kubeconfig", str(path), "config", "current-context"])
    actual = actual.strip()
    if actual != target:
        raise OpsError("context_mismatch", "Managed kubeconfig current context does not match target", 3)
    return target, path


def _kubectl_json(ctx, path, target, resource, namespace=None, extra=None):
    argv = ["kubectl", "--kubeconfig", str(path), "--context", target, "get", resource]
    if namespace:
        argv += ["--namespace", namespace]
    argv += (extra or []) + ["-o", "json"]
    return run_json(ctx, argv)


def _items(doc):
    return doc.get("items", []) if isinstance(doc, dict) else []


@operation("k8s.health", required=("target", "namespace"), executables=("kubectl",), runbook=True, timeout=45)
def k8s_health(ctx, args):
    target, path = ensure_target(ctx, args)
    namespace = _safe_value(args.get("namespace"), "Kubernetes namespace")
    nodes = _items(_kubectl_json(ctx, path, target, "nodes"))
    workloads = _items(_kubectl_json(ctx, path, target, "deployments,statefulsets,daemonsets", namespace))
    pods = _items(_kubectl_json(ctx, path, target, "pods", namespace))
    events = _items(_kubectl_json(ctx, path, target, "events", namespace, ["--field-selector", "type=Warning"]))
    ingresses = _items(_kubectl_json(ctx, path, target, "ingress", namespace))
    findings = []
    node_rows = []
    for obj in nodes:
        ready = next((c.get("status") == "True" for c in obj.get("status", {}).get("conditions", []) if c.get("type") == "Ready"), False)
        row = {"name": obj.get("metadata", {}).get("name"), "ready": ready}
        node_rows.append(row)
        if not ready:
            findings.append(finding("critical", "node_not_ready", "Node is not Ready", row["name"]))
    workload_rows = []
    for obj in workloads:
        meta, spec, status = obj.get("metadata", {}), obj.get("spec", {}), obj.get("status", {})
        desired = spec.get("replicas", status.get("desiredNumberScheduled", 0))
        ready = status.get("readyReplicas", status.get("numberReady", 0))
        row = {"kind": obj.get("kind"), "name": meta.get("name"), "desired": desired or 0, "ready": ready or 0}
        workload_rows.append(row)
        if row["ready"] < row["desired"]:
            findings.append(finding("critical", "workload_unavailable", "Workload has unavailable replicas", f"{row['kind']}/{row['name']}", row))
    phases, unhealthy = {}, []
    for obj in pods:
        meta, status = obj.get("metadata", {}), obj.get("status", {})
        phase = status.get("phase", "Unknown")
        phases[phase] = phases.get(phase, 0) + 1
        containers = status.get("containerStatuses", [])
        restarts = sum(x.get("restartCount", 0) for x in containers)
        waiting = [next(iter(x.get("state", {}).get("waiting", {}).values()), "") for x in containers if x.get("state", {}).get("waiting")]
        if phase not in ("Running", "Succeeded") or restarts > 5 or waiting:
            row = {"name": meta.get("name"), "phase": phase, "restarts": restarts, "waiting": waiting[:4]}
            unhealthy.append(row)
            findings.append(finding("critical" if phase == "Failed" else "warning", "pod_unhealthy", "Pod requires attention", f"Pod/{row['name']}", row))
    event_rows = []
    for obj in sorted(events, key=lambda x: x.get("lastTimestamp", x.get("metadata", {}).get("creationTimestamp", "")), reverse=True):
        event_rows.append({"reason": obj.get("reason"), "object": obj.get("involvedObject", {}).get("name"), "message": obj.get("message"), "count": obj.get("count", 1)})
    event_rows = bounded(event_rows, ctx, 10)
    if event_rows:
        findings.append(finding("warning", "warning_events", f"{len(event_rows)} recent warning events", namespace))
    metrics = {}
    try:
        node_metrics = run_json(ctx, ["kubectl", "--kubeconfig", str(path), "--context", target, "top", "nodes", "-o", "json"])
        pod_metrics = run_json(ctx, ["kubectl", "--kubeconfig", str(path), "--context", target, "top", "pods", "--namespace", namespace, "-o", "json"])
        metrics = {"nodes": len(_items(node_metrics)), "pods": len(_items(pod_metrics))}
    except OpsError as exc:
        findings.append(finding("info", "metrics_unavailable", "Resource metrics are unavailable", target, {"reason": exc.code}))
    status = "critical" if any(x["severity"] == "critical" for x in findings) else "degraded" if any(x["severity"] == "warning" for x in findings) else "healthy"
    return ctx.success(status=status, target={"id": target, "namespace": namespace}, summary={"nodes": len(nodes), "workloads": len(workloads), "pods": len(pods), "pod_phases": phases, "unhealthy_pods": len(unhealthy), "warnings": len(event_rows), "metrics": metrics}, findings=bounded(findings, ctx), data={"nodes": bounded(node_rows, ctx), "workloads": bounded(workload_rows, ctx), "unhealthy_pods": bounded(unhealthy, ctx), "warning_events": event_rows, "ingress": bounded([{"name": x.get("metadata", {}).get("name"), "hosts": [r.get("host") for r in x.get("spec", {}).get("rules", [])]} for x in ingresses], ctx)})


@operation("k8s.workload", required=("target", "namespace", "kind", "name"), executables=("kubectl",), runbook=True, timeout=30)
def k8s_workload(ctx, args):
    target, path = ensure_target(ctx, args)
    namespace = _safe_value(args.get("namespace"), "Kubernetes namespace")
    kind = _safe_value(args.get("kind"), "Kubernetes kind")
    name = _safe_value(args.get("name"), "Kubernetes workload name")
    allowed = {"deployment", "statefulset", "daemonset", "job", "cronjob"}
    if kind not in allowed:
        raise OpsError("invalid_target", "Explicit namespace, supported kind, and name are required", 3)
    obj = _kubectl_json(ctx, path, target, f"{kind}/{name}", namespace)
    status = obj.get("status", {})
    desired = obj.get("spec", {}).get("replicas", status.get("desiredNumberScheduled"))
    ready = status.get("readyReplicas", status.get("numberReady"))
    if kind == "job":
        failed = status.get("failed", 0)
        failure = next((condition for condition in status.get("conditions", []) if condition.get("type") == "Failed" and condition.get("status") == "True"), None)
        complete = next((condition for condition in status.get("conditions", []) if condition.get("type") == "Complete" and condition.get("status") == "True"), None)
        healthy = not failed and failure is None
        desired = obj.get("spec", {}).get("completions", 1)
        ready = status.get("succeeded", 0)
        findings = [] if healthy else [finding("critical", "job_failed", "Job has failed", f"{kind}/{name}", {"failed": failed, "reason": (failure or {}).get("reason")})]
        if complete is None and healthy:
            findings.append(finding("info", "job_in_progress", "Job has not completed", f"{kind}/{name}", {"succeeded": ready, "desired": desired}))
    elif kind == "cronjob":
        suspended = bool(obj.get("spec", {}).get("suspend", False))
        healthy = True
        desired = None
        ready = len(status.get("active", []))
        findings = [finding("info", "cronjob_suspended", "CronJob is suspended", f"{kind}/{name}")] if suspended else []
    else:
        healthy = desired is None or ready == desired
        findings = [] if healthy else [finding("critical", "workload_unavailable", "Workload has unavailable replicas", f"{kind}/{name}", {"desired": desired, "ready": ready})]
    labels = obj.get("spec", {}).get("selector", {}).get("matchLabels", {})
    pods = []
    if labels:
        selector = ",".join(f"{key}={value}" for key, value in sorted(labels.items()))
        pod_doc = _kubectl_json(ctx, path, target, "pods", namespace, ["--selector", selector])
        for pod in _items(pod_doc):
            pod_status = pod.get("status", {})
            pods.append({"name": pod.get("metadata", {}).get("name"), "phase": pod_status.get("phase"), "restarts": sum(x.get("restartCount", 0) for x in pod_status.get("containerStatuses", []))})
    event_doc = _kubectl_json(ctx, path, target, "events", namespace, ["--field-selector", f"type=Warning,involvedObject.name={name}"])
    events = bounded([{"reason": x.get("reason"), "message": x.get("message"), "count": x.get("count", 1)} for x in _items(event_doc)], ctx, 10)
    if events:
        findings.append(finding("warning", "workload_warning_events", "Workload has warning events", f"{kind}/{name}", {"count": len(events)}))
    result_status = "critical" if not healthy else "degraded" if events else "healthy"
    owners = [{"kind": x.get("kind"), "name": x.get("name"), "controller": x.get("controller", False)} for x in obj.get("metadata", {}).get("ownerReferences", [])]
    return ctx.success(status=result_status, target={"id": target, "namespace": namespace, "kind": kind, "name": name}, summary={"desired": desired, "ready": ready, "selected_pods": len(pods), "warnings": len(events)}, findings=findings, data={"conditions": bounded(status.get("conditions", []), ctx, 10), "owners": bounded(owners, ctx, 10), "pods": bounded(pods, ctx), "warning_events": events})


@operation("k8s.logs", required=("target", "namespace", "pod"), allowed=("container", "previous", "lines"), executables=("kubectl",), timeout=20)
def k8s_logs(ctx, args):
    target, path = ensure_target(ctx, args)
    namespace = _safe_value(args.get("namespace"), "Kubernetes namespace")
    pod = _safe_value(args.get("pod"), "Kubernetes pod")
    lines = min(max(int(args.get("lines", 50)), 1), 200)
    argv = ["kubectl", "--kubeconfig", str(path), "--context", target, "logs", pod, "--namespace", namespace, "--tail", str(lines)]
    if args.get("container"):
        argv += ["--container", _safe_value(args["container"], "Kubernetes container")]
    if args.get("previous"):
        argv.append("--previous")
    output, _, _ = run(ctx, argv)
    rows = output.splitlines()
    if len(rows) > lines:
        ctx.truncated, ctx.omitted = True, ctx.omitted + len(rows) - lines
        rows = rows[-lines:]
    return ctx.success(status="unknown", target={"id": target, "namespace": namespace, "pod": pod}, summary={"lines": len(rows), "previous": bool(args.get("previous"))}, data={"lines": rows})


def _argo_summary(app, ctx):
    status = app.get("status", {})
    sync = status.get("sync", {}).get("status", "Unknown")
    health = status.get("health", {}).get("status", "Unknown")
    destination = app.get("spec", {}).get("destination", {})
    base = {"name": app.get("metadata", {}).get("name"), "health": health, "sync": sync, "revision": status.get("sync", {}).get("revision"), "destination": {"server": destination.get("server"), "name": destination.get("name"), "namespace": destination.get("namespace")}}
    healthy = health == "Healthy" and sync == "Synced"
    details = {}
    findings = []
    if not healthy:
        problematic = []
        for resource in status.get("resources", []):
            rh = resource.get("health", {}).get("status", "Healthy")
            rs = resource.get("status", "Synced")
            if rh != "Healthy" or rs != "Synced":
                problematic.append({k: resource.get(k) for k in ("group", "kind", "namespace", "name", "status") if resource.get(k) is not None} | {"health": rh})
        operation_state = status.get("operationState", {})
        operation_data = {key: operation_state.get(key) for key in ("phase", "message", "startedAt", "finishedAt", "retryCount") if operation_state.get(key) is not None}
        sync_result = operation_state.get("syncResult", {})
        if sync_result:
            operation_data["sync_result"] = {"revision": sync_result.get("revision")}
            source = sync_result.get("source", {})
            safe_source = {key: source.get(key) for key in ("repoURL", "path", "chart", "targetRevision") if source.get(key) is not None}
            if safe_source:
                operation_data["sync_result"]["source"] = safe_source
            operation_data["resource_results"] = bounded([{key: row.get(key) for key in ("group", "kind", "namespace", "name", "status", "message") if row.get(key) is not None} for row in sync_result.get("resources", [])], ctx, 25)
        details = {"conditions": bounded(status.get("conditions", []), ctx, 10), "operation": operation_data, "problematic_resources": bounded(problematic, ctx, 25)}
        findings.append(finding("critical" if health in ("Degraded", "Missing") else "warning", "argo_app_unhealthy", f"Application is {health} / {sync}", base["name"], {"health": health, "sync": sync}))
    return base, details, findings, healthy


@operation("argo.app", required=("argocd_context", "app"), executables=("argocd",), runbook=True, timeout=30)
def argo_app(ctx, args):
    context = _safe_value(args.get("argocd_context"), "Argo CD context")
    app_name = _safe_value(args.get("app"), "Argo CD application")
    app = run_json(ctx, ["argocd", "--argocd-context", context, "app", "get", app_name, "-o", "json"])
    summary, details, findings, healthy = _argo_summary(app, ctx)
    return ctx.success(status="healthy" if healthy else "critical" if any(x["severity"] == "critical" for x in findings) else "degraded", target={"argocd_context": context, "app": app_name}, summary=summary, findings=findings, data=details)


@operation("argo.history", required=("argocd_context", "app"), allowed=("limit",), executables=("argocd",), runbook=True, timeout=30)
def argo_history(ctx, args):
    context = _safe_value(args.get("argocd_context"), "Argo CD context")
    app_name = _safe_value(args.get("app"), "Argo CD application")
    limit = min(max(int(args.get("limit", 10)), 1), 50)
    rows = run_json(ctx, ["argocd", "--argocd-context", context, "app", "history", app_name, "-o", "json"])
    if isinstance(rows, dict):
        rows = rows.get("items", rows.get("history", []))
    rows = bounded(rows if isinstance(rows, list) else [], ctx, limit)
    safe = [{k: row.get(k) for k in ("id", "revision", "deployedAt", "deployStartedAt") if k in row} for row in rows]
    return ctx.success(status="unknown", target={"argocd_context": context, "app": app_name}, summary={"revisions": len(safe)}, data={"history": safe})


@operation("argo.diff", required=("argocd_context", "app"), executables=("argocd",), runbook=True, timeout=30)
def argo_diff(ctx, args):
    context = _safe_value(args.get("argocd_context"), "Argo CD context")
    app_name = _safe_value(args.get("app"), "Argo CD application")
    output, _, code = run(ctx, ["argocd", "--argocd-context", context, "app", "diff", app_name], allowed_codes=(0, 1))
    resources = []
    for match in re.finditer(r"(?m)^=====\s+([^/\s]+)/([^/\s]+)/([^\s]+)", output):
        group, kind, name = match.groups()
        if kind.lower() != "secret":
            resources.append({"group": group, "kind": kind, "name": name})
    resources = bounded(resources, ctx, 50)
    return ctx.success(status="degraded" if code else "healthy", target={"argocd_context": context, "app": app_name}, summary={"different": bool(code), "resources": len(resources)}, findings=[finding("warning", "argo_diff", "Application has differences", app_name)] if code else [], data={"resources": resources})


def _refresh_if_needed(ctx, target):
    args = {"target": target["id"], "aws_profile": target["aws_profile"], "region": target["aws_region"], "cluster": target["eks_cluster"]}
    status = eks_status_data(target["id"])
    identity_changed = any((
        status.get("aws_profile") != target["aws_profile"],
        status.get("aws_region") != target["aws_region"],
        status.get("eks_cluster") != target["eks_cluster"],
    ))
    if status["stale"] or identity_changed:
        eks_refresh(ctx, args)


def _gitops_target(target, deadline=None):
    required = ("id", "argocd_context", "apps", "aws_profile", "aws_region", "eks_cluster", "namespace")
    if any(k not in target for k in required) or not isinstance(target.get("apps"), list) or not 1 <= len(target["apps"]) <= 20:
        raise OpsError("invalid_target", "Each GitOps target requires identity, AWS/EKS fields, Argo context, 1-20 apps, and namespace", 3)
    local = Context("gitops.target")
    local.deadline = deadline
    try:
        _target_id({"target": target["id"]})
        _refresh_if_needed(local, target)
        kh = k8s_health(local, {"target": target["id"], "namespace": target["namespace"]})
        apps, findings = [], list(kh["findings"])
        app_statuses = []
        for app_name in target["apps"]:
            result = argo_app(local, {"argocd_context": target["argocd_context"], "app": app_name})
            app_statuses.append(result["status"])
            apps.append({"app": app_name, "status": result["status"], "summary": result["summary"], **({"findings": result["findings"], "data": result["data"]} if result["status"] != "healthy" else {})})
            local.truncated = local.truncated or result["meta"]["truncated"]
            local.omitted += result["meta"]["omitted"]
            destination = result["summary"].get("destination", {})
            expected = target["eks_cluster"]
            observed = destination.get("name") or ""
            if observed and observed != expected:
                mismatch = finding("critical", "destination_mismatch", "Argo application destination does not match expected EKS cluster", app_name, {"expected": expected, "observed": observed})
                findings.append(mismatch)
        status = "critical" if "critical" in app_statuses or any(x["severity"] == "critical" for x in findings) else "degraded" if "degraded" in app_statuses or any(x["severity"] == "warning" for x in findings) else "healthy"
        return {"id": target["id"], "ok": True, "status": status, "kubernetes": kh["summary"], "apps": apps, "findings": findings, "commands_run": local.commands_run, "truncated": local.truncated, "omitted": local.omitted, "dropped_bytes": local.dropped_bytes}
    except OpsError as exc:
        return {"id": target.get("id"), "ok": False, "status": "error", "error": {"code": exc.code, "message": exc.message}, "commands_run": local.commands_run, "truncated": local.truncated, "omitted": local.omitted, "dropped_bytes": local.dropped_bytes}


@operation("gitops.multicluster", required=("targets",), allowed=("concurrency", "fail_fast"), executables=("aws", "kubectl", "argocd"), runbook=True, mutation="local_cache", timeout=300)
def gitops_multicluster(ctx, args):
    targets = args.get("targets")
    if not isinstance(targets, list) or not 1 <= len(targets) <= 20:
        raise OpsError("invalid_target", "Multicluster operation requires 1-20 explicit targets", 3)
    ids = [x.get("id") for x in targets if isinstance(x, dict)]
    if len(ids) != len(set(ids)):
        raise OpsError("invalid_target", "Target IDs must be unique", 3)
    workers = min(max(int(args.get("concurrency", 4)), 1), 4)
    if args.get("fail_fast"):
        results = []
        for target in targets:
            result = _gitops_target(target, ctx.deadline)
            results.append(result)
            if not result["ok"]:
                break
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda target: _gitops_target(target, ctx.deadline), targets))
    ctx.commands_run += sum(x.pop("commands_run", 0) for x in results)
    ctx.truncated = ctx.truncated or any(x.pop("truncated", False) for x in results)
    ctx.omitted += sum(x.pop("omitted", 0) for x in results)
    ctx.dropped_bytes += sum(x.pop("dropped_bytes", 0) for x in results)
    failed = sum(not x["ok"] for x in results)
    critical = sum(x["status"] == "critical" for x in results)
    status = "critical" if critical else "degraded" if failed else "healthy"
    findings = [finding("critical", "target_failed", "Target diagnostic failed", x.get("id"), x.get("error", {})) for x in results if not x["ok"]]
    return ctx.success(status=status, target={"target_ids": ids}, summary={"targets": len(results), "healthy": sum(x["status"] == "healthy" for x in results), "critical": critical, "failed": failed}, findings=findings, data={"targets": results})


def _chart(args):
    chart = pathlib.Path(args.get("chart", "")).expanduser()
    if not chart.is_dir():
        raise OpsError("unsafe_chart", "Chart must be an existing local directory", 3)
    return chart.resolve()


def _helm_base(args):
    chart = _chart(args)
    release = _safe_value(args.get("release"), "Helm release")
    namespace = _safe_value(args.get("namespace"), "Helm namespace")
    values = args.get("values", []) or []
    argv = ["helm", "template", release, str(chart), "--namespace", namespace]
    for path in values:
        value = pathlib.Path(path).expanduser().resolve()
        if not value.is_file():
            raise OpsError("invalid_input", "Values path must be a local file", 3)
        argv += ["--values", str(value)]
    return chart, argv


def _manifest_summary(text, ctx):
    resources = []
    for doc in text.split("\n---"):
        kind = re.search(r"(?m)^kind:\s*([^\s#]+)", doc)
        name = re.search(r"(?ms)^metadata:\s*\n(?:^[ \t]+.*\n)*?^[ \t]+name:\s*([^\s#]+)", doc)
        if kind and name and kind.group(1).lower() != "secret":
            resources.append({"kind": kind.group(1), "name": name.group(1)})
    resources.sort(key=lambda x: (x["kind"], x["name"]))
    return bounded(resources, ctx, 100), hashlib.sha256(text.encode()).hexdigest()


@operation("helm.check", required=("chart", "release", "namespace"), allowed=("values",), executables=("helm",), runbook=True, timeout=60)
def helm_check(ctx, args):
    chart, template = _helm_base(args)
    lint_args = ["helm", "lint", str(chart)]
    for value in args.get("values", []) or []:
        lint_args += ["--values", str(pathlib.Path(value).expanduser().resolve())]
    lint, _, _ = run(ctx, lint_args, timeout=60)
    rendered, _, _ = run(ctx, template, timeout=60)
    resources, digest = _manifest_summary(rendered, ctx)
    counts = {}
    for row in resources:
        counts[row["kind"]] = counts.get(row["kind"], 0) + 1
    return ctx.success(target={"chart": str(chart), "release": args["release"], "namespace": args["namespace"]}, summary={"resources": len(resources), "kinds": counts, "manifest_sha256": digest}, data={"lint": lint.splitlines()[-10:]})


@operation("helm.render-summary", required=("chart", "release", "namespace"), allowed=("values",), executables=("helm",), runbook=True, timeout=60)
def helm_render(ctx, args):
    chart, template = _helm_base(args)
    rendered, _, _ = run(ctx, template, timeout=60)
    resources, digest = _manifest_summary(rendered, ctx)
    return ctx.success(target={"chart": str(chart), "release": args["release"], "namespace": args["namespace"]}, summary={"resources": len(resources), "manifest_sha256": digest}, data={"resources": resources})


def _engine(args):
    engine = args.get("engine")
    if engine not in ("docker", "podman"):
        raise OpsError("invalid_target", "Engine must be docker or podman", 3)
    return engine


@operation("container.status", required=("engine", "name"), executables=("docker|podman",), runbook=True)
def container_status(ctx, args):
    engine, name = _engine(args), _safe_value(args.get("name"), "Container name")
    rows = run_json(ctx, [engine, "inspect", name])
    obj = rows[0] if isinstance(rows, list) and rows else rows
    state = obj.get("State", {})
    data = {"name": obj.get("Name", name).lstrip("/"), "image": obj.get("Config", {}).get("Image"), "status": state.get("Status"), "running": state.get("Running"), "exit_code": state.get("ExitCode"), "restart_count": obj.get("RestartCount", 0), "health": state.get("Health", {}).get("Status"), "ports": sorted((obj.get("NetworkSettings", {}).get("Ports") or {}).keys())}
    healthy = data["running"] and data["health"] not in ("unhealthy",)
    return ctx.success(status="healthy" if healthy else "critical", target={"engine": engine, "name": name}, summary=data, findings=[] if healthy else [finding("critical", "container_unhealthy", "Container is not healthy and running", name, data)])


@operation("container.logs", required=("engine", "name"), allowed=("lines",), executables=("docker|podman",))
def container_logs(ctx, args):
    engine, name = _engine(args), _safe_value(args.get("name"), "Container name")
    lines = min(max(int(args.get("lines", 50)), 1), 200)
    output, _, _ = run(ctx, [engine, "logs", "--tail", str(lines), name])
    rows = output.splitlines()[-lines:]
    return ctx.success(status="unknown", target={"engine": engine, "name": name}, summary={"lines": len(rows)}, data={"lines": rows})


@operation("compose.check", required=("engine", "file", "project"), executables=("docker|podman",), runbook=True, timeout=30)
def compose_check(ctx, args):
    engine, file, project = _engine(args), pathlib.Path(args.get("file", "")).expanduser().resolve(), _safe_value(args.get("project"), "Compose project")
    if not file.is_file() or not project:
        raise OpsError("invalid_target", "Explicit local Compose file and project are required", 3)
    base = [engine, "compose", "--file", str(file), "--project-name", project]
    run(ctx, base + ["config", "--quiet"])
    output, _, _ = run(ctx, base + ["ps", "--format", "json"])
    try:
        parsed = json.loads(output)
        rows = parsed if isinstance(parsed, list) else [parsed]
    except json.JSONDecodeError:
        rows = [json.loads(line) for line in output.splitlines() if line.strip()]
    safe = bounded([{"name": x.get("Name"), "service": x.get("Service"), "state": x.get("State"), "health": x.get("Health"), "exit_code": x.get("ExitCode")} for x in rows], ctx)
    bad = [x for x in safe if x["state"] not in ("running", "Running") or x["health"] == "unhealthy"]
    return ctx.success(status="critical" if bad else "healthy", target={"engine": engine, "file": str(file), "project": project}, summary={"services": len(safe), "unhealthy": len(bad)}, findings=[finding("critical", "compose_unhealthy", "Compose services require attention", project, {"count": len(bad)})] if bad else [], data={"services": safe})


@operation("service.status", required=("unit",), executables=("systemctl",), runbook=True)
def service_status(ctx, args):
    unit = _safe_value(args.get("unit"), "systemd unit")
    fields = "Id,LoadState,ActiveState,SubState,Result,MainPID,ExecMainStatus,NRestarts,UnitFileState"
    output, _, _ = run(ctx, ["systemctl", "show", unit, "--property", fields, "--no-pager"])
    data = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    healthy = data.get("LoadState") == "loaded" and data.get("ActiveState") == "active"
    return ctx.success(status="healthy" if healthy else "critical", target={"unit": unit}, summary=data, findings=[] if healthy else [finding("critical", "service_unhealthy", "Service is not active", unit, data)])


@operation("service.logs", required=("unit",), allowed=("since", "lines"), executables=("journalctl",))
def service_logs(ctx, args):
    unit = _safe_value(args.get("unit"), "systemd unit")
    lines = min(max(int(args.get("lines", 50)), 1), 200)
    since = _safe_value(args.get("since", "1 hour ago"), "journal time range")
    output, _, _ = run(ctx, ["journalctl", "--unit", unit, "--since", since, "--lines", str(lines), "--output", "json", "--no-pager"])
    rows = []
    for line in output.splitlines():
        try:
            obj = json.loads(line)
            rows.append({"timestamp": obj.get("__REALTIME_TIMESTAMP"), "priority": obj.get("PRIORITY"), "message": obj.get("MESSAGE"), "pid": obj.get("_PID")})
        except json.JSONDecodeError:
            continue
    return ctx.success(status="unknown", target={"unit": unit}, summary={"entries": len(rows)}, data={"entries": bounded(rows, ctx, lines)})


def _dns_name(packet, offset):
    labels, end, seen = [], None, set()
    while True:
        if offset >= len(packet) or offset in seen:
            raise OpsError("malformed_output", "DNS response contained an invalid name", 6)
        seen.add(offset)
        length = packet[offset]
        if length == 0:
            offset += 1
            return ".".join(labels), end or offset
        if length & 0xC0 == 0xC0:
            if offset + 1 >= len(packet):
                raise OpsError("malformed_output", "DNS response contained a truncated pointer", 6)
            pointer = ((length & 0x3F) << 8) | packet[offset + 1]
            end = end or offset + 2
            offset = pointer
            continue
        offset += 1
        if length > 63 or offset + length > len(packet):
            raise OpsError("malformed_output", "DNS response contained an invalid label", 6)
        labels.append(packet[offset:offset + length].decode("idna"))
        offset += length


def _dns_query(host, record_type):
    host = host.rstrip(".")
    labels = host.split(".")
    if not host or len(host) > 253 or any(not label or len(label.encode("idna")) > 63 for label in labels):
        raise OpsError("invalid_target", "DNS hostname is invalid", 3)
    qtypes = {"A": 1, "CNAME": 5, "MX": 15, "TXT": 16, "AAAA": 28}
    query_id = int.from_bytes(os.urandom(2), "big")
    qname = b"".join(bytes((len(encoded),)) + encoded for encoded in (label.encode("idna") for label in labels)) + b"\0"
    packet = struct.pack("!HHHHHH", query_id, 0x0100, 1, 0, 0, 0) + qname + struct.pack("!HH", qtypes[record_type], 1)
    nameserver = None
    try:
        for line in pathlib.Path("/etc/resolv.conf").read_text().splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0] == "nameserver":
                nameserver = fields[1]
                break
    except OSError:
        pass
    if not nameserver:
        raise OpsError("dns_failed", "No DNS nameserver is configured", 5)
    family = socket.AF_INET6 if ":" in nameserver else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.settimeout(5)
            sock.sendto(packet, (nameserver, 53))
            response, _ = sock.recvfrom(65535)
    except OSError as exc:
        raise OpsError("dns_failed", "DNS query failed", 5, {"reason": str(exc)})
    if len(response) < 12:
        raise OpsError("malformed_output", "DNS response was truncated", 6)
    response_id, flags, questions, answers, _, _ = struct.unpack("!HHHHHH", response[:12])
    if response_id != query_id or flags & 0x000F:
        raise OpsError("dns_failed", "DNS server returned an error", 5, {"rcode": flags & 0x000F})
    offset = 12
    for _ in range(questions):
        _, offset = _dns_name(response, offset)
        offset += 4
    records = []
    for _ in range(answers):
        _, offset = _dns_name(response, offset)
        if offset + 10 > len(response):
            raise OpsError("malformed_output", "DNS answer was truncated", 6)
        kind, klass, _, length = struct.unpack("!HHIH", response[offset:offset + 10])
        offset += 10
        start, end = offset, offset + length
        if end > len(response):
            raise OpsError("malformed_output", "DNS record data was truncated", 6)
        if klass == 1 and kind == qtypes[record_type]:
            if kind == 1 and length == 4:
                records.append(socket.inet_ntop(socket.AF_INET, response[start:end]))
            elif kind == 28 and length == 16:
                records.append(socket.inet_ntop(socket.AF_INET6, response[start:end]))
            elif kind == 5:
                records.append(_dns_name(response, start)[0])
            elif kind == 15 and length >= 3:
                records.append({"preference": struct.unpack("!H", response[start:start + 2])[0], "exchange": _dns_name(response, start + 2)[0]})
            elif kind == 16:
                values, cursor = [], start
                while cursor < end:
                    size = response[cursor]
                    cursor += 1
                    values.append(response[cursor:cursor + size].decode("utf-8", "replace"))
                    cursor += size
                records.append("".join(values))
        offset = end
    return records


@operation("network.dns", required=("host",), allowed=("type",), runbook=True)
def network_dns(ctx, args):
    host, record_type = args.get("host"), args.get("type", "A")
    if record_type not in ("A", "AAAA", "CNAME", "MX", "TXT"):
        raise OpsError("unsupported_record_type", "DNS record type is unsupported", 2)
    records = _dns_query(_safe_value(host, "DNS host"), record_type)
    records = bounded(sorted(records, key=lambda x: json.dumps(x, sort_keys=True)), ctx, 50)
    return ctx.success(target={"host": host, "type": record_type}, summary={"records": len(records)}, data={"records": records})


def _resolve_public_http_host(host):
    try:
        addresses = {row[4][0] for row in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise OpsError("dns_failed", "HTTP target DNS lookup failed", 5, {"reason": str(exc)})
    if not addresses:
        raise OpsError("dns_failed", "HTTP target resolved to no addresses", 5)
    public = []
    for address in sorted(addresses):
        try:
            if not ipaddress.ip_address(address).is_global:
                raise OpsError("unsafe_url", "HTTP target resolves to a non-public address", 3)
            public.append(address)
        except ValueError:
            raise OpsError("unsafe_url", "HTTP target resolved to an invalid address", 3)
    return public[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, port, timeout):
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self):
        raw = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


@operation("network.http", required=("url",), allowed=("method",))
def network_http(ctx, args):
    url, method = args.get("url"), args.get("method", "HEAD")
    try:
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except (TypeError, ValueError):
        raise OpsError("unsafe_url", "URL is malformed", 3)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or method not in ("HEAD", "GET"):
        raise OpsError("unsafe_url", "URL must be HTTP(S) without credentials and method HEAD or GET", 3)
    address = _resolve_public_http_host(parsed.hostname)
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    host_header = parsed.hostname if port in (80, 443) else f"{parsed.hostname}:{port}"
    connection = _PinnedHTTPSConnection(parsed.hostname, address, port, 10) if parsed.scheme == "https" else http.client.HTTPConnection(address, port=port, timeout=10)
    try:
        connection.request(method, path, headers={"Host": host_header, "User-Agent": "agent-ops/1"})
        response = connection.getresponse()
        body = response.read(65537) if method == "GET" else b""
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        raise OpsError("http_failed", "HTTP diagnostic failed", 5, {"reason": str(exc)})
    finally:
        connection.close()
    if len(body) > 65536:
        body = body[:65536]
        ctx.truncated = True
    safe_headers = {k.lower(): v for k, v in response.getheaders() if k.lower() in ("content-type", "content-length", "location", "server", "cache-control")}
    return ctx.success(status="healthy" if response.status < 400 else "degraded", target={"url": url, "method": method}, summary={"status": response.status, "headers": safe_headers, "body_bytes": len(body), "body_sha256": hashlib.sha256(body).hexdigest() if body else None})


@operation("network.tls", required=("host",), allowed=("port", "server_name"), runbook=True)
def network_tls(ctx, args):
    host = _safe_value(args.get("host"), "TLS host")
    port = int(args.get("port", 443))
    server_name = _safe_value(args.get("server_name") or host, "TLS server name")
    if not 1 <= port <= 65535:
        raise OpsError("invalid_target", "TLS port must be 1-65535", 3)
    try:
        with socket.create_connection((host, port), timeout=10) as raw:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=server_name) as conn:
                cert = conn.getpeercert()
                cipher = conn.cipher()
    except (OSError, ssl.SSLError) as exc:
        raise OpsError("tls_failed", "TLS verification failed", 5, {"reason": str(exc)})
    subject = dict(x[0] for x in cert.get("subject", []))
    issuer = dict(x[0] for x in cert.get("issuer", []))
    return ctx.success(target={"host": host, "port": port, "server_name": server_name}, summary={"subject": subject.get("commonName"), "issuer": issuer.get("commonName"), "not_before": cert.get("notBefore"), "not_after": cert.get("notAfter"), "cipher": cipher[0] if cipher else None})


def _project(repo):
    if not isinstance(repo, str) or "/" not in repo or repo.startswith("/") or repo.endswith("/"):
        raise OpsError("invalid_target", "Repository must be namespace/project", 3)
    return urllib.parse.quote(repo, safe="")


@operation("ci.status", required=("host", "repo"), allowed=("ref",), executables=("glab",), runbook=True, timeout=30)
def ci_status(ctx, args):
    host, project = _safe_value(args.get("host"), "GitLab host"), _project(args.get("repo"))
    endpoint = f"projects/{project}/pipelines?per_page=10"
    if args.get("ref"):
        endpoint += "&ref=" + urllib.parse.quote(args["ref"], safe="")
    rows = run_json(ctx, ["glab", "api", "--hostname", host, endpoint])
    safe = bounded([{k: x.get(k) for k in ("id", "status", "ref", "sha", "web_url", "updated_at")} for x in rows], ctx, 10)
    bad = [x for x in safe if x.get("status") in ("failed", "canceled")]
    return ctx.success(status="degraded" if bad else "healthy", target={"host": host, "repo": args["repo"], "ref": args.get("ref")}, summary={"pipelines": len(safe), "failed": len(bad), "latest": safe[0] if safe else None}, findings=[finding("warning", "pipeline_failed", "Recent pipeline failed", str(x.get("id")), {"ref": x.get("ref")}) for x in bad], data={"pipelines": safe})


@operation("ci.failures", required=("host", "repo", "pipeline"), executables=("glab",), runbook=True, timeout=30)
def ci_failures(ctx, args):
    host, project, pipeline = _safe_value(args.get("host"), "GitLab host"), _project(args.get("repo")), _safe_value(str(args.get("pipeline")), "GitLab pipeline")
    if not re.fullmatch(r"[1-9][0-9]*", pipeline):
        raise OpsError("invalid_target", "GitLab pipeline must be a positive integer", 3)
    rows = run_json(ctx, ["glab", "api", "--hostname", host, f"projects/{project}/pipelines/{pipeline}/jobs?scope[]=failed&per_page=50"])
    safe = bounded([{k: x.get(k) for k in ("id", "name", "stage", "status", "failure_reason", "web_url", "duration")} for x in rows], ctx, 50)
    return ctx.success(status="critical" if safe else "healthy", target={"host": host, "repo": args["repo"], "pipeline": pipeline}, summary={"failed_jobs": len(safe)}, findings=[finding("critical", "job_failed", "GitLab job failed", x.get("name"), {"stage": x.get("stage"), "reason": x.get("failure_reason")}) for x in safe], data={"jobs": safe})
