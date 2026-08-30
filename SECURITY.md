# Security Policy

## Supported versions

Security fixes are applied to the latest released version and the current default branch.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability or include real credentials, kubeconfigs, auth configuration, production output, or other sensitive data in a report. Use GitHub's private vulnerability reporting for the repository at `pandew-home/agentic-reuse`. If private reporting is unavailable, contact the maintainer through a private channel listed on the maintainer's GitHub profile.

Include a minimal sanitized reproduction, affected version, expected safety boundary, and impact. Replace hosts, account identifiers, tokens, and infrastructure names with synthetic values.

## Security boundary

`agent-ops` invokes local executables and may make diagnostic network requests. Users are responsible for the credentials and authorization those external tools use. The project aims to constrain commands, require explicit targets, bound output, redact common secret patterns, and reject mutating runbooks, but redaction is defense in depth rather than a guarantee that arbitrary upstream output contains no sensitive information.

The only intended mutation is creation of managed EKS kubeconfig cache files under `~/.local/state/agent-ops/` with restrictive permissions. Remote infrastructure mutation and modification of `~/.kube/config` are not supported.
