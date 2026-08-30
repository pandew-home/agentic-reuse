import argparse
import json
import os
import shutil
import sys

from . import __version__
from .core import Context, OpsError, dump, error_envelope, run
from . import operations  # noqa: F401
from .registry import REGISTRY, describe
from .runbooks import discover, execute, load_file, resolve


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        target_flags = ("--target", "--namespace", "--argocd-context", "--app", "--host", "--repo", "--unit", "--engine", "--name")
        target_error = "required" in message and any(flag in message for flag in target_flags)
        raise OpsError("invalid_target" if target_error else "invalid_arguments", message, 3 if target_error else 2)


def _parser():
    parser = JsonArgumentParser(prog="agent-ops", description="Safe compact DevOps diagnostics")
    parser.add_argument("--version", action="version", version=f"agent-ops {__version__}")
    sub = parser.add_subparsers(dest="group", required=True)
    sub.add_parser("doctor")
    sub.add_parser("operations")

    eks = sub.add_parser("eks").add_subparsers(dest="action", required=True)
    p = eks.add_parser("status"); p.add_argument("--target", required=True)
    p = eks.add_parser("refresh"); _target(p); p.add_argument("--aws-profile", required=True); p.add_argument("--region", required=True); p.add_argument("--cluster", required=True)

    k8s = sub.add_parser("k8s").add_subparsers(dest="action", required=True)
    p = k8s.add_parser("health"); _target(p); p.add_argument("--namespace", required=True)
    p = k8s.add_parser("workload"); _target(p); p.add_argument("--namespace", required=True); p.add_argument("--kind", required=True); p.add_argument("--name", required=True)
    p = k8s.add_parser("logs"); _target(p); p.add_argument("--namespace", required=True); p.add_argument("--pod", required=True); p.add_argument("--container"); p.add_argument("--previous", action="store_true"); p.add_argument("--lines", type=int, default=50)

    argo = sub.add_parser("argo").add_subparsers(dest="action", required=True)
    for action in ("app", "history", "diff"):
        p = argo.add_parser(action); p.add_argument("--argocd-context", required=True); p.add_argument("--app", required=True)
        if action == "history": p.add_argument("--limit", type=int, default=10)

    helm = sub.add_parser("helm").add_subparsers(dest="action", required=True)
    for action in ("check", "render-summary"):
        p = helm.add_parser(action); p.add_argument("--chart", required=True); p.add_argument("--release", required=True); p.add_argument("--namespace", required=True); p.add_argument("--values", action="append", default=[])

    container = sub.add_parser("container").add_subparsers(dest="action", required=True)
    for action in ("status", "logs"):
        p = container.add_parser(action); p.add_argument("--engine", required=True, choices=("docker", "podman")); p.add_argument("--name", required=True)
        if action == "logs": p.add_argument("--lines", type=int, default=50)
    compose = sub.add_parser("compose").add_subparsers(dest="action", required=True)
    p = compose.add_parser("check"); p.add_argument("--engine", required=True, choices=("docker", "podman")); p.add_argument("--file", required=True); p.add_argument("--project", required=True)

    service = sub.add_parser("service").add_subparsers(dest="action", required=True)
    p = service.add_parser("status"); p.add_argument("--unit", required=True)
    p = service.add_parser("logs"); p.add_argument("--unit", required=True); p.add_argument("--since", default="1 hour ago"); p.add_argument("--lines", type=int, default=50)

    network = sub.add_parser("network").add_subparsers(dest="action", required=True)
    p = network.add_parser("dns"); p.add_argument("--host", required=True); p.add_argument("--type", default="A", choices=("A", "AAAA", "CNAME", "MX", "TXT"))
    p = network.add_parser("http"); p.add_argument("--url", required=True); p.add_argument("--method", default="HEAD", choices=("HEAD", "GET"))
    p = network.add_parser("tls"); p.add_argument("--host", required=True); p.add_argument("--port", type=int, default=443); p.add_argument("--server-name")

    ci = sub.add_parser("ci").add_subparsers(dest="action", required=True)
    p = ci.add_parser("status"); p.add_argument("--host", required=True); p.add_argument("--repo", required=True); p.add_argument("--ref")
    p = ci.add_parser("failures"); p.add_argument("--host", required=True); p.add_argument("--repo", required=True); p.add_argument("--pipeline", required=True)

    runbook = sub.add_parser("runbook").add_subparsers(dest="action", required=True)
    p = runbook.add_parser("list"); p.add_argument("--scope", choices=("user", "project", "all"), default="all")
    p = runbook.add_parser("validate"); p.add_argument("--file", required=True)
    p = sub.add_parser("run"); p.add_argument("name"); p.add_argument("--scope", choices=("user", "project")); p.add_argument("--param", action="append", default=[])
    return parser


def _target(parser):
    parser.add_argument("--target", required=True)


def _args(namespace):
    values = vars(namespace).copy()
    values.pop("group", None); values.pop("action", None)
    return values


def _parse_params(values):
    result = {}
    for value in values:
        if "=" not in value:
            raise OpsError("invalid_parameter", "Runbook parameters use NAME=VALUE", 2)
        key, raw = value.split("=", 1)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
        if isinstance(parsed, (dict, list)):
            raise OpsError("invalid_parameter", "Runbook parameters must be scalar", 2)
        result[key] = parsed
    return result


def main(argv=None):
    parser = _parser()
    ctx = Context("cli")
    try:
        ns = parser.parse_args(argv)
    except OpsError as exc:
        sys.stdout.write(dump(error_envelope(ctx, exc)) + "\n")
        return exc.exit_code
    except SystemExit as exc:
        return exc.code
    operation_name = ns.group if ns.group in ("doctor", "operations", "run") else f"{ns.group}.{ns.action}"
    ctx = Context(operation_name)
    try:
        if ns.group == "doctor":
            probes = {
                "aws": ["aws", "--version"],
                "kubectl": ["kubectl", "version", "--client=true", "--output=json"],
                "argocd": ["argocd", "version", "--client", "--short"],
                "helm": ["helm", "version", "--short"],
                "docker": ["docker", "--version"],
                "podman": ["podman", "--version"],
                "systemctl": ["systemctl", "--version"],
                "journalctl": ["journalctl", "--version"],
                "glab": ["glab", "--version"],
            }
            tools = []
            for name, command in probes.items():
                available, version = shutil.which(name) is not None, None
                if available:
                    try:
                        stdout, stderr, _ = run(ctx, command, timeout=5)
                        version = next((line.strip() for line in (stdout + "\n" + stderr).splitlines() if line.strip()), None)
                    except OpsError:
                        pass
                tools.append({"name": name, "available": available, "version": version})
            result = ctx.success(status="healthy", summary={"available": sum(x["available"] for x in tools), "total": len(tools)}, data={"tools": tools})
        elif ns.group == "operations":
            result = ctx.success(summary={"operations": len(REGISTRY)}, data={"operations": describe()})
        elif ns.group == "runbook" and ns.action == "list":
            rows = discover(ns.scope)
            result = ctx.success(status="unknown" if not rows else "healthy", summary={"runbooks": len(rows)}, data={"runbooks": rows})
        elif ns.group == "runbook" and ns.action == "validate":
            doc = load_file(ns.file)
            result = ctx.success(summary={"valid": True, "name": doc["name"], "steps": len(doc["steps"])}, target={"file": os.path.abspath(os.path.expanduser(ns.file))})
        elif ns.group == "run":
            source = resolve(ns.name, ns.scope)
            doc = load_file(source["path"])
            result = execute(ctx, doc, _parse_params(ns.param))
            result["target"].update({"scope": source["scope"], "source": source["path"]})
        else:
            op = REGISTRY[operation_name]
            result = op.handler(ctx, _args(ns))
        sys.stdout.write(dump(result) + "\n")
        return 0
    except OpsError as exc:
        sys.stdout.write(dump(error_envelope(ctx, exc)) + "\n")
        return exc.exit_code
    except Exception as exc:
        failure = OpsError("internal_error", "Unexpected diagnostic failure", 5, {"type": type(exc).__name__, "message": str(exc)})
        sys.stdout.write(dump(error_envelope(ctx, failure)) + "\n")
        return failure.exit_code
