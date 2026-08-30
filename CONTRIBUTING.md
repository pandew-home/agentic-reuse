# Contributing

Contributions should increase reuse and reduce repeated agent token consumption without weakening the safety boundary.

## Development setup

Use Python 3.10 or newer. The runtime must remain Python standard-library only.

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
python3 -m compileall -q src tests
python3 -m unittest discover -s tests -v
```

## Design expectations

- Prefer an existing operation, then a constrained runbook, before adding Python.
- Add operations only for stable, repeated diagnostic workflows with deterministic compact output.
- Require explicit targets and fixed argument vectors. Never use `shell=True`, implicit contexts, or unknown flag forwarding.
- Keep remote operations read-only. Local managed EKS kubeconfig cache updates are the only accepted mutation class.
- Bound subprocess time, captured bytes, strings, lines, collection sizes, and concurrency.
- Parse machine-readable upstream output when available and emit the shared `agent-ops/v1` envelope.
- Redact credentials and secret-like fields before values leave the process.
- Do not add fixtures containing credentials, kubeconfigs, auth files, local state, or production output.
- Update the operation list in `skills/agent-ops/SKILL.md` when the CLI surface changes.

Tests should use `unittest`, temporary directories, and fake executables prepended to `PATH`. Cover success, invalid targets, authentication errors, timeouts, malformed output, truncation, redaction, and mutation refusal as relevant.

By contributing, you agree that your contribution is licensed under the MIT License.
