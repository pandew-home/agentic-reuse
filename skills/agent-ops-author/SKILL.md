---
name: agent-ops-author
description: Optimize token use by converting repeated safe DevOps reasoning and verbose command sequences into reusable agent-ops scripts or constrained runbooks. Prefer reuse and composition before adding code. Allow the agent to add new operation modules in the same structure when a reuse opportunity spans multiple sessions. Do not use for Git or remote mutation.
---

# Agent Ops Author

The primary goal is to spend tokens once on a reusable implementation instead of repeatedly constructing commands, parsing noisy output, and rediscovering the same diagnostic workflow.

Extend a source checkout without redesigning the library. Use the checkout supplied by the caller or clone `https://github.com/pandew-home/agentic-reuse.git`; do not infer a checkout from a pip/pipx launcher. Inspect `<checkout>/src/agent_ops/`, `<checkout>/tests/`, `agent-ops operations`, and the usage skill before editing. Never edit site-packages. Preserve concurrent changes.

## Compose First

1. Identify repetition: commands rebuilt across sessions, verbose output repeatedly analyzed, duplicated fallback logic, and stable facts agents repeatedly extract.
2. Record current output bytes and lines, sensitive fields, and the compact facts actually needed.
3. Reuse an existing operation when possible. Otherwise compose registered operations in a constrained JSON runbook.
4. Add Python only when composition cannot express the reusable workflow, and only as a new module under `<checkout>/src/agent_ops/` following the existing structure (see "Adding a new operation module"). Never create one-off shell wrappers, edit site-packages, or auto-load repository Python plugins.

## Safety Boundary

Classify additions as read-only diagnostics or managed EKS kubeconfig cache mutation. Reject remote deploy, sync, apply, restart, scale, rollback, delete, exec, port-forward, follow, pull/build/up, retry/cancel/play/approve, discovery, arbitrary command passthrough, `shell=True`, unknown flag forwarding, and implicit contexts. Git remains `agent-git`.

Every registered operation defines its public name, required explicit targets, fixed argv, parser, compact summary, timeout, byte/item bounds, runbook eligibility, and mutation class. Use standard-library Python, machine-readable upstream output, `agent_ops.core.run`, the shared envelope, and stable findings/errors.

Required exit classes are `0` completed, `2` usage/schema, `3` safety/target, `4` dependency/auth, `5` execution/timeout, and `6` malformed upstream output.

Bound strings, bytes, lines, items, subprocess duration, and concurrency. Set `meta.truncated` and `meta.omitted`. Never return kubeconfig contents, AWS/Kion credentials, environment dumps, registry auth, Kubernetes Secret data, or CLI auth configuration. Redact secret-like keys, auth headers, credential URLs, AWS key patterns, environment assignments, and private keys before errors leave the process.

## Adding a new operation module

When the `meta` feedback or repeated sessions show a stable, read-only diagnostic workflow that recurs across multiple sessions and cannot be expressed as a runbook of existing operations, add a new module under `<checkout>/src/agent_ops/` following the same structure as `operations.py`. This is the only sanctioned way to extend the tool surface; never write one-off shell wrappers or auto-load repository plugins.

### When to add one

- The same diagnostic is rebuilt across sessions — the `meta` signal shows high `ingested_bytes` against a small `envelope_bytes` with repeated calls.
- No existing operation covers the domain, and a runbook of existing operations cannot express it.
- It is read-only diagnostics, or the single accepted mutation class (managed EKS kubeconfig cache). Reject everything else.

### Module structure

Create `src/agent_ops/<domain>.py`:

```python
from .core import Context, OpsError, bounded, finding, run, run_json
from .registry import operation


@operation("domain.action", required=("target",), allowed=("extra",), executables=("kubectl",), runbook=True, timeout=30)
def domain_action(ctx, args):
    target = args.get("target")
    if not isinstance(target, str) or not target or target.startswith("-"):
        raise OpsError("invalid_target", "Target must be an explicit, non-flag value", 3)
    argv = ["kubectl", "--context", target, "get", "thing", "-o", "json"]
    doc = run_json(ctx, argv)  # fixed argv, shell=False
    if not isinstance(doc, dict) or not isinstance(doc.get("items"), list):
        raise OpsError("malformed_output", "Upstream response missing items list", 6)
    items = bounded(doc["items"], ctx)
    findings = [finding("warning", "thing_unhealthy", "Thing needs attention", target, {})] if not items else []
    status = "degraded" if findings else "healthy"
    return ctx.success(status=status, target={"id": target}, summary={"count": len(items)}, findings=findings, data={"items": items})
```

Rules:

- Decorate every entry point with `@operation`. The name is `domain.action` (lower-case, dotted). `required` lists explicit targets; `allowed` lists the only other accepted args; `executables` lists external tools; `runbook=True` makes it composable; `mutation="none"` (or `"local_cache"` only for EKS) with a `timeout`.
- Build fixed argument vectors through `core.run`/`run_json` (`shell=False`). Never use `shell=True`, implicit contexts, or unknown flag forwarding.
- Validate every target and reject bad input with `OpsError("invalid_target", ..., 3)`, mirroring the `_target_id`/`_safe_value` guards in `operations.py`.
- Bound output with `bounded()` and let `meta.truncated`/`meta.omitted` be set; rely on `sanitize`/`redact_text` for redaction — never return secrets.
- Return `ctx.success(...)` so the shared `agent-ops/v1` envelope and `meta` metrics are produced automatically.

### Wiring registration

The registry populates from `@operation` decorators at import time, so the new module must be imported. Add one line to `operations.py`:

```python
from . import <domain>  # registers domain.* operations
```

Do not mutate `REGISTRY` by hand. After import, `agent-ops operations` lists the new entries.

### Tests and rollout

Add cases to `tests/test_agent_ops.py` using the existing `FAKE` executable harness: success, auth failure (`fake-auth`), timeout (`fake-timeout`), malformed (`fake-malformed`), oversized (`fake-noisy`), redaction, and mutation refusal. Run the verification block below; only after it passes, update the command list in `skills/agent-ops/SKILL.md` and confirm agent discovery.

## Blocking Tests

Use `unittest` and fake executables prepended to `PATH`. Cover success, auth failure, timeout, malformed/noisy/oversized output, secrets, truncation, partial capability, omitted targets, unknown flags, and mutation rejection. Fixtures contain no credentials, kubeconfigs, production output, or auth files.

Use the runtime `meta` feedback to decide when reuse pays off. Every `agent-ops` call reports `captured_bytes`, `dropped_bytes`, `ingested_bytes`, and `envelope_bytes` in `meta`. A recurring diagnostic with high `ingested_bytes` against a small `envelope_bytes` is a candidate for a runbook (compose first) or a new registered operation. Do not fabricate savings; rely on these measured fields and the blocking tests below.

Before exposing an operation, run:

```bash
cd <checkout>
PYTHONPYCACHEPREFIX=/tmp/agentic-reuse-compile python3 -m compileall -q src tests
PYTHONPATH=src python3 -m unittest discover -s tests -v
agent-ops operations
```

Only after tests pass, update the usage skill operation list and verify Kilo, Grok, and Claude discovery.
