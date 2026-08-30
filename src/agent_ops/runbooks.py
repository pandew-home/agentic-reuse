import concurrent.futures
import json
import os
import pathlib
import re

from .core import Context, OpsError, SECRET_KEY_RE, error_envelope
from . import operations as _operations  # Register built-in operations for direct module use.
from .registry import REGISTRY

SCHEMA = "agent-ops/runbook-v1"
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
SUB_RE = re.compile(r"^\$\{([a-z][a-z0-9_]*)\}$")
INTEGER_ARGS = {"lines", "limit", "port", "concurrency"}
BOOLEAN_ARGS = {"previous", "fail_fast"}
LIST_ARGS = {"values", "targets"}
CHOICES = {
    ("network.dns", "type"): {"A", "AAAA", "CNAME", "MX", "TXT"},
    ("network.http", "method"): {"HEAD", "GET"},
    ("container.status", "engine"): {"docker", "podman"},
    ("container.logs", "engine"): {"docker", "podman"},
    ("compose.check", "engine"): {"docker", "podman"},
}


def _project_root(start=None):
    current = pathlib.Path(start or os.getcwd()).resolve()
    for parent in (current, *current.parents):
        if (parent / ".git").exists():
            return parent
    return current


def roots():
    user = pathlib.Path(os.path.expanduser("~/.config/agent-ops/runbooks"))
    project = _project_root() / ".agent-ops" / "runbooks"
    return user, project


def discover(scope="all"):
    user, project = roots()
    found = {}
    selected = [("user", user)] if scope == "user" else [("project", project)] if scope == "project" else [("user", user), ("project", project)]
    for label, root in selected:
        if root.is_dir():
            for path in sorted(root.glob("*.json")):
                if NAME_RE.fullmatch(path.stem):
                    found[path.stem] = {"name": path.stem, "scope": label, "path": str(path)}
    return [found[name] for name in sorted(found)]


def _no_secrets(value, path=""):
    if isinstance(value, dict):
        for key, child in value.items():
            if SECRET_KEY_RE.search(str(key)):
                raise OpsError("unsafe_runbook", f"Secret-like field is not allowed: {path}{key}", 3)
            _no_secrets(child, f"{path}{key}.")
    elif isinstance(value, list):
        for child in value:
            _no_secrets(child, path)


def load_file(path):
    path = pathlib.Path(path).expanduser().resolve()
    try:
        if path.stat().st_size > 1_000_000:
            raise OpsError("invalid_runbook", "Runbook exceeds one megabyte", 2)
        doc = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise OpsError("invalid_runbook", "Runbook could not be read as JSON", 2, {"reason": str(exc)})
    validate(doc)
    return doc


def validate(doc):
    if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
        raise OpsError("invalid_runbook", f"Runbook schema must be {SCHEMA}", 2)
    if not NAME_RE.fullmatch(str(doc.get("name", ""))):
        raise OpsError("invalid_runbook", "Runbook name is invalid", 2)
    steps = doc.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 20:
        raise OpsError("invalid_runbook", "Runbook requires 1-20 steps", 2)
    concurrency = doc.get("concurrency", 1)
    if not isinstance(concurrency, int) or not 1 <= concurrency <= 4:
        raise OpsError("invalid_runbook", "Runbook concurrency must be 1-4", 2)
    if not isinstance(doc.get("fail_fast", False), bool):
        raise OpsError("invalid_runbook", "Runbook fail_fast must be boolean", 2)
    parameters = doc.get("parameters", {})
    if not isinstance(parameters, dict) or any(not NAME_RE.fullmatch(str(k)) for k in parameters):
        raise OpsError("invalid_runbook", "Runbook parameters must be named scalar defaults", 2)
    if any(isinstance(v, (dict, list)) for v in parameters.values()):
        raise OpsError("invalid_runbook", "Runbook parameter defaults must be scalar", 2)
    _no_secrets(doc)
    ids = set()
    for step in steps:
        if not isinstance(step, dict) or not NAME_RE.fullmatch(str(step.get("id", ""))) or step["id"] in ids:
            raise OpsError("invalid_runbook", "Step IDs must be unique and valid", 2)
        ids.add(step["id"])
        name = step.get("operation")
        if name not in REGISTRY or not REGISTRY[name].runbook:
            raise OpsError("unsafe_runbook", f"Operation is not runbook eligible: {name}", 3)
        args = step.get("args", {})
        if not isinstance(args, dict):
            raise OpsError("invalid_runbook", "Step args must be an object", 2)
        for required in REGISTRY[name].required:
            if required not in args:
                raise OpsError("invalid_runbook", f"Step {step['id']} is missing {required}", 2)
        unknown = sorted(set(args) - set(REGISTRY[name].allowed))
        if unknown:
            raise OpsError("invalid_runbook", f"Step {step['id']} has unknown arguments: {', '.join(unknown)}", 2)
        for key, value in args.items():
            if key in INTEGER_ARGS and not isinstance(value, int) and not (isinstance(value, str) and SUB_RE.fullmatch(value)):
                raise OpsError("invalid_runbook", f"Step {step['id']} argument {key} must be an integer", 2)
            if key in BOOLEAN_ARGS and not isinstance(value, bool) and not (isinstance(value, str) and SUB_RE.fullmatch(value)):
                raise OpsError("invalid_runbook", f"Step {step['id']} argument {key} must be boolean", 2)
            if key in LIST_ARGS and not isinstance(value, list):
                raise OpsError("invalid_runbook", f"Step {step['id']} argument {key} must be a list", 2)
            if key not in INTEGER_ARGS | BOOLEAN_ARGS | LIST_ARGS and not isinstance(value, (str, int)):
                raise OpsError("invalid_runbook", f"Step {step['id']} argument {key} must be scalar", 2)
            choices = CHOICES.get((name, key))
            if choices and not (isinstance(value, str) and SUB_RE.fullmatch(value)) and value not in choices:
                raise OpsError("invalid_runbook", f"Step {step['id']} argument {key} is unsupported", 2)
        _validate_substitutions(args, parameters)
        if name == "gitops.multicluster":
            targets = args.get("targets")
            if not isinstance(targets, list) or not 1 <= len(targets) <= 20:
                raise OpsError("invalid_runbook", "Multicluster steps require 1-20 targets", 2)
            target_ids = []
            required_target = {"id", "argocd_context", "apps", "aws_profile", "aws_region", "eks_cluster", "namespace"}
            for target in targets:
                if not isinstance(target, dict) or set(target) != required_target:
                    raise OpsError("invalid_runbook", "Each multicluster target must contain only the required target fields", 2)
                if not all(isinstance(target[key], str) and target[key] for key in required_target - {"apps"}):
                    raise OpsError("invalid_runbook", "Multicluster target identity fields must be non-empty strings", 2)
                if not isinstance(target["apps"], list) or not 1 <= len(target["apps"]) <= 20 or not all(isinstance(app, str) and app for app in target["apps"]):
                    raise OpsError("invalid_runbook", "Each multicluster target requires 1-20 application names", 2)
                target_ids.append(target["id"])
            if len(target_ids) != len(set(target_ids)):
                raise OpsError("invalid_runbook", "Multicluster target IDs must be unique", 2)
    return doc


def _validate_substitutions(value, parameters):
    if isinstance(value, dict):
        for child in value.values():
            _validate_substitutions(child, parameters)
    elif isinstance(value, list):
        for child in value:
            _validate_substitutions(child, parameters)
    elif isinstance(value, str):
        if "$" in value:
            match = SUB_RE.fullmatch(value)
            if not match or match.group(1) not in parameters:
                raise OpsError("invalid_runbook", "Only whole-value declared parameter substitutions are allowed", 2)


def substitute(value, parameters):
    if isinstance(value, dict):
        return {k: substitute(v, parameters) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, parameters) for v in value]
    if isinstance(value, str):
        match = SUB_RE.fullmatch(value)
        if match:
            return parameters[match.group(1)]
    return value


def resolve(name, scope=None):
    if not NAME_RE.fullmatch(name):
        raise OpsError("invalid_runbook", "Runbook name is invalid", 2)
    if scope == "project":
        matches = {x["name"]: x for x in discover("project")}
    elif scope == "user":
        matches = {x["name"]: x for x in discover("user")}
    else:
        user_matches = {x["name"]: x for x in discover("user")}
        project_matches = {x["name"]: x for x in discover("project")}
        if name in project_matches and name not in user_matches:
            raise OpsError("project_scope_required", "Project runbooks require --scope project", 3)
        matches = user_matches
    if name not in matches:
        raise OpsError("runbook_not_found", f"Runbook not found: {name}", 2)
    return matches[name]


def _run_step(step, parameters, deadline):
    operation = REGISTRY[step["operation"]]
    local = Context(operation.name, deadline=deadline)
    args = substitute(step["args"], parameters)
    validate({"schema": SCHEMA, "name": "resolved-step", "steps": [{"id": step["id"], "operation": step["operation"], "args": args}]})
    try:
        result = operation.handler(local, args)
    except OpsError as exc:
        result = error_envelope(local, exc)
    compact = {
        "id": step["id"],
        "operation": result["operation"],
        "ok": result["ok"],
        "status": result["status"],
        "target": result["target"],
        "summary": result["summary"],
        "meta": result["meta"],
    }
    if result["status"] != "healthy" or not result["ok"]:
        compact["findings"] = result["findings"]
        compact["data"] = result["data"]
        if "error" in result:
            compact["error"] = result["error"]
    return compact


def execute(ctx, doc, supplied=None):
    validate(doc)
    parameters = dict(doc.get("parameters", {}))
    for key, value in (supplied or {}).items():
        if key not in parameters:
            raise OpsError("invalid_parameter", f"Unknown runbook parameter: {key}", 2)
        parameters[key] = value
    if doc.get("fail_fast", False):
        results = []
        for step in doc["steps"]:
            result = _run_step(step, parameters, ctx.deadline)
            results.append(result)
            if not result["ok"]:
                break
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=doc.get("concurrency", 1)) as pool:
            futures = [pool.submit(_run_step, step, parameters, ctx.deadline) for step in doc["steps"]]
            results = [future.result() for future in futures]
    for result in results:
        ctx.commands_run += result["meta"]["commands_run"]
        ctx.truncated = ctx.truncated or result["meta"]["truncated"]
        ctx.omitted += result["meta"]["omitted"]
        ctx.dropped_bytes += result["meta"]["dropped_bytes"]
        ctx.captured_bytes += result["meta"]["captured_bytes"]
    statuses = [x["status"] for x in results]
    status = "critical" if "critical" in statuses else "degraded" if any(x in ("degraded", "error") for x in statuses) else "unknown" if "unknown" in statuses else "healthy"
    findings = []
    for result in results:
        for item in result.get("findings", []):
            findings.append({**item, "resource": f"{result['id']}:{item.get('resource', '')}".rstrip(":")})
        if not result["ok"]:
            findings.append({"severity": "critical", "code": "runbook_step_failed", "message": result.get("error", {}).get("message", "Runbook step failed"), "resource": result["id"], "evidence": {"error_code": result.get("error", {}).get("code")}})
    return ctx.success(status=status, target={"runbook": doc["name"]}, summary={"steps": len(results), "failed": sum(not x["ok"] for x in results)}, findings=findings[:50], data={"steps": results})
