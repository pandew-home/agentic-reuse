---
name: agent-ops
description: Reduce repeated token use by reusing compact agent-ops scripts and runbooks instead of reconstructing verbose DevOps command sequences. Covers Kubernetes, Argo CD, EKS, Helm, containers, systemd, network/TLS, and GitLab CI. Do not use for Git or remote infrastructure mutation.
allowed-tools: Bash(agent-ops:*)
---

# Agent Ops

The primary purpose of `agent-ops` is token optimization through script reuse. Prefer an existing operation or runbook whenever an agent would otherwise reconstruct a DevOps command sequence, parse verbose output, repeat fallback logic, or reason over the same workflow again.

Each operation centralizes command construction, parsing, bounds, redaction, and summarization. It returns one compact `agent-ops/v1` JSON object so agents consume findings instead of raw command output.

Do not reproduce an available operation with direct CLI calls. Run `agent-ops operations` when syntax or coverage is uncertain.

Prefer `agent-ops run NAME` for repeatable multi-step checks. Runbooks reuse registered scripts without arbitrary shell; project runbooks shadow same-named user runbooks.

Supply every explicit target. Never substitute the current kube context, Argo context, GitLab host, or repository.

Read output in this order: `ok`, `status`, `summary`, `findings`, then only relevant `data` branches. Treat `unknown` as missing evidence, not health. Do not send output through lossy RTK rewriting.

## Commands

```text
agent-ops doctor
agent-ops operations
agent-ops runbook list --scope user|project|all
agent-ops runbook validate --file PATH
agent-ops run NAME [--param NAME=VALUE]
agent-ops eks refresh --target ID --aws-profile PROFILE --region REGION --cluster CLUSTER
agent-ops eks status --target ID
agent-ops k8s health --target ID --namespace NS
agent-ops k8s workload --target ID --namespace NS --kind KIND --name NAME
agent-ops k8s logs --target ID --namespace NS --pod POD [--container NAME] [--previous] [--lines N]
agent-ops argo app --argocd-context CONTEXT --app APP
agent-ops argo history --argocd-context CONTEXT --app APP [--limit N]
agent-ops argo diff --argocd-context CONTEXT --app APP
agent-ops helm check --chart PATH --release NAME --namespace NS [--values PATH]
agent-ops helm render-summary --chart PATH --release NAME --namespace NS [--values PATH]
agent-ops container status --engine docker|podman --name NAME
agent-ops container logs --engine docker|podman --name NAME [--lines N]
agent-ops compose check --engine docker|podman --file PATH --project NAME
agent-ops service status --unit UNIT
agent-ops service logs --unit UNIT [--since DURATION] [--lines N]
agent-ops network dns --host HOST [--type A|AAAA|CNAME|MX|TXT]
agent-ops network http --url URL [--method HEAD|GET]
agent-ops network tls --host HOST [--port PORT] [--server-name NAME]
agent-ops ci status --host HOST --repo NAMESPACE/PROJECT [--ref REF]
agent-ops ci failures --host HOST --repo NAMESPACE/PROJECT --pipeline ID
```

Multicluster runbooks declare each target's `id`, `argocd_context`, `apps`, `aws_profile`, `aws_region`, `eks_cluster`, and `namespace`. They never discover clusters or applications.

`gitops.multicluster` is a runbook-only registered operation with `targets`, optional `concurrency`, and optional `fail_fast`; it may refresh stale managed kubeconfig caches before diagnostics.

The only mutation is managed EKS kubeconfig generation under `~/.local/state/agent-ops/`; it does not alter clusters or `~/.kube/config`.

If a repeated workflow is missing, use `agent-ops-author` to compose a runbook first or add one reusable central operation. Do not solve repeated workflows with one-off shell snippets. Continue using `agent-git` for Git. Never bypass a refusal with mutating direct commands.
