# agentic-reuse

`agentic-reuse` packages the `agent-ops` command and `agent_ops` Python package: a standard-library toolkit for token-efficient, repeatable DevOps diagnostics. It replaces reconstructed command chains and verbose raw output with reusable scripts, constrained runbooks, bounded parsing, redaction, and one compact `agent-ops/v1` JSON envelope.

The package focuses on spending reasoning tokens once. Agents can invoke a stable operation or runbook, inspect `summary` and `findings`, and avoid repeatedly rediscovering command flags, fallback behavior, parsing rules, and safety checks.

## Installation

Install the isolated CLI with pipx:

```bash
pipx install agentic-reuse
agent-ops --version
```

Or install it with pip:

```bash
python3 -m pip install agentic-reuse
```

For local development:

```bash
python3 -m pip install -e .
```

Python 3.10 or newer is required. The Python package has no third-party runtime dependencies. Individual operations call explicit external tools such as `aws`, `kubectl`, `argocd`, `helm`, Docker or Podman, `systemctl`, `journalctl`, and `glab`; run `agent-ops doctor` to inspect availability.

## Operations

Discover the current operation surface and its required arguments:

```bash
agent-ops operations
agent-ops doctor
```

Examples:

```bash
agent-ops k8s health --target prod-use1 --namespace platform
agent-ops argo app --argocd-context central --app storefront
agent-ops helm check --chart ./chart --release storefront --namespace platform
agent-ops container status --engine docker --name web
agent-ops network tls --host example.com --port 443
agent-ops ci status --host gitlab.example.com --repo team/project --ref main
```

Every completed invocation writes one compact JSON object. Read `ok`, `status`, `summary`, and `findings` first; inspect only relevant `data` branches when more evidence is needed.

## Safety

`agent-ops` is designed for bounded diagnostics, not general automation:

- Operations use fixed argument vectors with `shell=False`; there is no arbitrary command passthrough.
- Kubernetes, Argo CD, GitLab, and similar contexts must be supplied explicitly. The tool does not infer production targets.
- Output sizes, item counts, subprocess durations, and multicluster concurrency are bounded.
- Secret-like structured fields and common credential patterns are redacted from returned output and errors.
- Runbooks can invoke only registered, runbook-eligible operations and reject secret-like fields.
- Remote deploy, apply, sync, restart, scale, rollback, delete, exec, and other mutating workflows are outside the safety boundary.
- The sole supported mutation is local managed EKS kubeconfig cache generation under `~/.local/state/agent-ops/`. It does not alter `~/.kube/config` or a remote cluster.

The repository contains no local state, kubeconfigs, credentials, production output, or generated caches. See [SECURITY.md](SECURITY.md) for reporting guidance.

## Runbooks

User runbooks are discovered at:

```text
~/.config/agent-ops/runbooks/*.json
```

Project runbooks are discovered from the repository root at:

```text
.agent-ops/runbooks/*.json
```

Project runbooks shadow user runbooks with the same name. List, validate, and execute them with:

```bash
agent-ops runbook list --scope all
agent-ops runbook validate --file .agent-ops/runbooks/site-check.json
agent-ops run site-check --param host=example.com
```

A minimal `agent-ops/runbook-v1` document looks like this:

```json
{
  "schema": "agent-ops/runbook-v1",
  "name": "site-check",
  "parameters": {
    "host": "example.com"
  },
  "fail_fast": false,
  "concurrency": 1,
  "steps": [
    {
      "id": "dns",
      "operation": "network.dns",
      "args": {
        "host": "${host}",
        "type": "A"
      }
    },
    {
      "id": "tls",
      "operation": "network.tls",
      "args": {
        "host": "${host}",
        "port": 443
      }
    }
  ]
}
```

Substitutions must occupy the entire value and reference declared scalar parameters. Runbooks contain one to twenty steps and cannot include arbitrary shell commands or secret-like keys.

## Agent Skills

The repository includes usage and authoring skills in `skills/agent-ops/` and `skills/agent-ops-author/`. Install or update both shared copies under `~/.agents/skills` and Claude copies under `~/.claude/skills` with:

```bash
./install-skills.sh
```

The installer updates only those two named skill directories. It is idempotent and does not remove unrelated skills or files.

## Development

Run compile checks and the standard-library test suite from the repository root:

```bash
python3 -m compileall -q src tests
python3 -m unittest discover -s tests -v
```

Tests prepend `src/` to `sys.path`, use temporary state, and provide fake executables. They do not require live infrastructure, credentials, kubeconfigs, or production output. Python 3.10 through 3.13 is tested in CI.

See [CONTRIBUTING.md](CONTRIBUTING.md) for operation design and safety expectations.
