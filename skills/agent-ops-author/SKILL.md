---
name: agent-ops-author
description: Optimize token use by converting repeated safe DevOps reasoning and verbose command sequences into reusable agent-ops scripts or constrained runbooks. Prefer reuse and composition before adding code. Do not use for Git or remote mutation.
---

# Agent Ops Author

The primary goal is to spend tokens once on a reusable implementation instead of repeatedly constructing commands, parsing noisy output, and rediscovering the same diagnostic workflow.

Extend a source checkout without redesigning the library. Use the checkout supplied by the caller or clone `https://github.com/pandew-home/agentic-reuse.git`; do not infer a checkout from a pip/pipx launcher. Inspect `<checkout>/src/agent_ops/`, `<checkout>/tests/`, `agent-ops operations`, and the usage skill before editing. Never edit site-packages. Preserve concurrent changes.

## Compose First

1. Identify repetition: commands rebuilt across sessions, verbose output repeatedly analyzed, duplicated fallback logic, and stable facts agents repeatedly extract.
2. Record current output bytes and lines, sensitive fields, and the compact facts actually needed.
3. Reuse an existing operation when possible. Otherwise compose registered operations in a constrained JSON runbook.
4. Add Python only when composition cannot express the reusable workflow, and only under `<checkout>/src/agent_ops/`. Never create one-off shell wrappers, edit site-packages, or auto-load repository Python plugins.

## Safety Boundary

Classify additions as read-only diagnostics or managed EKS kubeconfig cache mutation. Reject remote deploy, sync, apply, restart, scale, rollback, delete, exec, port-forward, follow, pull/build/up, retry/cancel/play/approve, discovery, arbitrary command passthrough, `shell=True`, unknown flag forwarding, and implicit contexts. Git remains `agent-git`.

Every registered operation defines its public name, required explicit targets, fixed argv, parser, compact summary, timeout, byte/item bounds, runbook eligibility, and mutation class. Use standard-library Python, machine-readable upstream output, `agent_ops.core.run`, the shared envelope, and stable findings/errors.

Required exit classes are `0` completed, `2` usage/schema, `3` safety/target, `4` dependency/auth, `5` execution/timeout, and `6` malformed upstream output.

Bound strings, bytes, lines, items, subprocess duration, and concurrency. Set `meta.truncated` and `meta.omitted`. Never return kubeconfig contents, AWS/Kion credentials, environment dumps, registry auth, Kubernetes Secret data, or CLI auth configuration. Redact secret-like keys, auth headers, credential URLs, AWS key patterns, environment assignments, and private keys before errors leave the process.

## Blocking Tests

Use `unittest` and fake executables prepended to `PATH`. Cover success, auth failure, timeout, malformed/noisy/oversized output, secrets, truncation, partial capability, omitted targets, unknown flags, and mutation rejection. Fixtures contain no credentials, kubeconfigs, production output, or auth files.

Measure canonical compact JSON bytes and lines against bounded raw output. Require meaningful reduction or document deterministic-reasoning value.

Before exposing an operation, run:

```bash
cd <checkout>
PYTHONPYCACHEPREFIX=/tmp/agentic-reuse-compile python3 -m compileall -q src tests
PYTHONPATH=src python3 -m unittest discover -s tests -v
agent-ops operations
```

Only after tests pass, update the usage skill operation list and verify Kilo, Grok, and Claude discovery.
