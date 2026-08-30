---
name: agent-ops-author
description: Spend tokens once by turning repeated safe DevOps diagnostics into reusable agent-ops runbooks or operations. Prefer reuse/runbook before code; add a module only for cross-session reuse. Read-only diagnostics only; no Git, no remote mutation.
---

# Agent Ops Author

Turn repeated diagnostics into reusable code so the agent spends tokens once, not every session. Work in a source checkout (`<checkout>`, cloned from the repo or supplied by the caller); never edit site-packages or infer a checkout from a pip/pipx launcher. Read `src/agent_ops/`, `tests/`, `agent-ops operations`, and the usage skill first.

## Decision ladder

1. **Reuse** an existing operation (`agent-ops operations`) when it fits.
2. **Compose** a constrained JSON runbook of existing operations for a repeatable multi-step check.
3. **Add a module** only when a stable, read-only diagnostic recurs across sessions and a runbook cannot express it (see below).

Never write one-off shell wrappers or auto-load repository plugins.

## Safety boundary

Additions are **read-only diagnostics** or the single accepted mutation (managed EKS kubeconfig cache). Reject: deploy/sync/apply/restart/scale/rollback/delete/exec/port-forward/follow/build-up/retry/approve, discovery, arbitrary passthrough, `shell=True`, unknown flag forwarding, and implicit contexts. Git stays in `agent-git`.

Every operation: explicit targets, fixed argv via `core.run` (`shell=False`), machine-readable upstream output, bounded strings/bytes/lines/items/duration/concurrency, `meta.truncated`/`meta.omitted` set, redaction via `sanitize`/`redact_text`, and the shared `agent-ops/v1` envelope. Exit codes: `0` ok, `2` usage/schema, `3` safety/target, `4` dependency/auth, `5` timeout/exec, `6` malformed upstream. Never return kubeconfigs, credentials, env dumps, Secret data, or auth config; redact secret keys/headers/URLs/keys before errors leave the process.

## Add a module

When `meta` shows high `ingested_bytes` vs small `envelope_bytes` across repeated calls and no operation covers it, create `src/agent_ops/<domain>.py`:

```python
from .core import Context, OpsError, bounded, finding, run, run_json
from .registry import operation


@operation("domain.action", required=("target",), allowed=("extra",), executables=("kubectl",), runbook=True, timeout=30)
def domain_action(ctx, args):
    target = args.get("target")
    if not isinstance(target, str) or not target or target.startswith("-"):
        raise OpsError("invalid_target", "Target must be an explicit, non-flag value", 3)
    doc = run_json(ctx, ["kubectl", "--context", target, "get", "thing", "-o", "json"])  # fixed argv, shell=False
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
        raise OpsError("malformed_output", "Upstream response missing items list", 6)
    items = bounded(doc["items"], ctx)
    findings = [finding("warning", "thing_unhealthy", "Thing needs attention", target, {})] if not items else []
    return ctx.success(status="degraded" if findings else "healthy", target={"id": target}, summary={"count": len(items)}, findings=findings, data={"items": items})
```

Rules: name is `domain.action` (lower-case, dotted); `required` = explicit targets, `allowed` = other args, `executables` = tools, `runbook=True` for composability, `mutation="none"` (or `"local_cache"` for EKS), with a `timeout`. Validate targets via `OpsError("invalid_target", ..., 3)`. Return `ctx.success(...)` so the envelope and `meta` metrics are produced automatically.

**Register**: import once in `operations.py` — `from . import <domain>` — so the `@operation` decorators populate `REGISTRY`. Do not edit `REGISTRY` directly.

## Verify

Add `tests/test_agent_ops.py` cases using the `FAKE` executable harness: success, auth (`fake-auth`), timeout (`fake-timeout`), malformed (`fake-malformed`), oversized (`fake-noisy`), redaction, mutation refusal. Then from `<checkout>`:

```bash
PYTHONPYCACHEPREFIX=/tmp/agentic-reuse-compile python3 -m compileall -q src tests
PYTHONPATH=src python3 -m unittest discover -s tests -v
agent-ops operations
```

Only after tests pass, update `skills/agent-ops/SKILL.md` command list and confirm agent discovery. Use `meta` to pick what to extract next; do not fabricate savings.
