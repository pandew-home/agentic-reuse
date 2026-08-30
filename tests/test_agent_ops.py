import contextlib
import io
import json
import os
import pathlib
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from agent_ops import cli, operations
from agent_ops.core import Context, OpsError, error_envelope, redact_text, run, run_json, sanitize
from agent_ops.registry import REGISTRY
from agent_ops.runbooks import validate


FAKE = r'''#!/usr/bin/env python3
import json, os, sys, time
name = os.path.basename(sys.argv[0])
mode = name.split("fake-", 1)[1] if name.startswith("fake-") else "success"
if mode == "timeout":
    time.sleep(5)
if mode == "auth":
    print("Authorization: Bearer never-a-real-token", file=sys.stderr)
    sys.exit(1)
if mode == "malformed":
    print("not-json")
    sys.exit(0)
if name == "docker":
    print(json.dumps([{"Name":"/web","Config":{"Image":"example/web:1","Env":["PASSWORD=never-real"]},"State":{"Status":"running","Running":True,"ExitCode":0,"Health":{"Status":"healthy"}},"RestartCount":1,"NetworkSettings":{"Ports":{"8080/tcp":[]}}}]))
elif name == "argocd":
    print(json.dumps({"metadata":{"name":"demo"},"spec":{"destination":{"name":"prod-use1","namespace":"platform"}},"status":{"health":{"status":"Healthy"},"sync":{"status":"Synced","revision":"abc123"},"resources":[]}}))
elif name == "glab":
    print(json.dumps([{"id":42,"status":"success","ref":"main","sha":"abc","web_url":"https://git.example/p/42","updated_at":"2026-01-01"}]))
elif name == "aws":
    path = sys.argv[sys.argv.index("--kubeconfig") + 1]
    alias = sys.argv[sys.argv.index("--alias") + 1]
    with open(path, "w") as f:
        f.write("apiVersion: v1\nclusters:\n- name: %s\ncontexts:\n- name: %s\nusers:\n- name: %s\ncurrent-context: %s\n" % (alias, alias, alias, alias))
    print("updated")
elif name == "kubectl":
    if "current-context" in sys.argv:
        print(os.environ.get("FAKE_TARGET", "demo"))
    else:
        print(json.dumps({"items":[]}))
else:
    print(json.dumps({"ok": True, "token": "never-real"}))
'''


class AgentOpsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name in ("docker", "argocd", "glab", "aws", "kubectl", "fake", "fake-auth", "fake-malformed", "fake-timeout"):
            path = self.bin / name
            path.write_text(FAKE)
            path.chmod(0o755)
        self.env = mock.patch.dict(os.environ, {"PATH": f"{self.bin}:{os.environ.get('PATH', '')}", "FAKE_MODE": "success", "FAKE_TARGET": "demo"})
        self.env.start()
        self.state = self.root / "state"
        self.kubes = self.state / "kubeconfigs"
        self.state_patch = mock.patch.multiple(operations, STATE_HOME=self.state, KUBECONFIG_HOME=self.kubes, EKS_STATE=self.state / "eks-state.json")
        self.state_patch.start()

    def tearDown(self):
        self.state_patch.stop()
        self.env.stop()
        self.temp.cleanup()

    def call_cli(self, argv):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = cli.main(argv)
        return code, json.loads(output.getvalue())

    def test_registry_has_full_v1_surface(self):
        expected = {"eks.refresh", "k8s.health", "argo.app", "gitops.multicluster", "helm.check", "container.status", "compose.check", "service.status", "network.tls", "ci.status"}
        self.assertTrue(expected.issubset(REGISTRY))
        self.assertEqual(21, len(REGISTRY))

    def test_cli_emits_one_stable_envelope(self):
        code, result = self.call_cli(["operations"])
        self.assertEqual(0, code)
        self.assertEqual({"schema", "ok", "status", "operation", "target", "summary", "findings", "data", "meta"}, set(result))
        self.assertEqual("agent-ops/v1", result["schema"])

    def test_cli_usage_error_is_json(self):
        code, result = self.call_cli(["k8s", "health"])
        self.assertEqual(3, code)
        self.assertFalse(result["ok"])
        self.assertEqual("invalid_target", result["error"]["code"])

    def test_argv_like_resource_name_is_rejected(self):
        with self.assertRaises(OpsError) as caught:
            operations.container_status(Context("container.status"), {"engine": "docker", "name": "--all"})
        self.assertEqual("invalid_target", caught.exception.code)

    def test_redaction_patterns(self):
        raw = "Authorization: Bearer abc PASSWORD=hunter2 AKIAABCDEFGHIJKLMNOP https://me:pass@example.test\n-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----"
        cleaned = redact_text(raw)
        for secret in ("abc", "hunter2", "AKIAABCDEFGHIJKLMNOP", "me:pass", "\nx\n"):
            self.assertNotIn(secret, cleaned)
        self.assertIn("[REDACTED]", cleaned)

    def test_structured_secret_keys_are_redacted(self):
        value = sanitize({"token": "x", "nested": {"password": "y"}, "safe": "z"})
        self.assertEqual("[REDACTED]", value["token"])
        self.assertEqual("[REDACTED]", value["nested"]["password"])
        self.assertEqual("z", value["safe"])

    def test_json_is_parsed_before_structured_redaction(self):
        data = run_json(Context("test"), ["fake"])
        self.assertEqual("never-real", data["token"])
        self.assertEqual("[REDACTED]", sanitize(data)["token"])

    def test_auth_failure_redacts_stderr(self):
        with self.assertRaises(OpsError) as caught:
            run(Context("test"), ["fake-auth"])
        self.assertEqual("authentication_failed", caught.exception.code)
        self.assertNotIn("never-a-real-token", caught.exception.message)

    def test_malformed_json_has_structured_failure(self):
        with self.assertRaises(OpsError) as caught:
            run_json(Context("test"), ["fake-malformed"])
        self.assertEqual(6, caught.exception.exit_code)
        result = error_envelope(Context("test"), caught.exception)
        self.assertEqual("malformed_output", result["error"]["code"])

    def test_timeout_terminates_command(self):
        started = time.monotonic()
        with self.assertRaises(OpsError) as caught:
            run(Context("test"), ["fake-timeout"], timeout=0.05)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual("command_timeout", caught.exception.code)

    def test_eks_refresh_isolated_and_mode_0600(self):
        ctx = Context("eks.refresh")
        result = operations.eks_refresh(ctx, {"target": "demo", "aws_profile": "kion-prod", "region": "us-east-1", "cluster": "prod-use1"})
        path = self.kubes / "demo.yaml"
        self.assertTrue(path.is_file())
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertNotIn(path.read_text(), json.dumps(result))
        state = json.loads((self.state / "eks-state.json").read_text())
        self.assertNotIn("credential", json.dumps(state).lower())

    def test_argo_healthy_output_is_compact(self):
        result = operations.argo_app(Context("argo.app"), {"argocd_context": "central", "app": "demo"})
        self.assertEqual("healthy", result["status"])
        self.assertEqual({}, result["data"])
        self.assertEqual([], result["findings"])

    def test_container_does_not_return_environment(self):
        result = operations.container_status(Context("container.status"), {"engine": "docker", "name": "web"})
        encoded = json.dumps(result)
        self.assertNotIn("PASSWORD", encoded)
        self.assertNotIn("never-real", encoded)
        self.assertEqual("healthy", result["status"])

    def test_ci_always_passes_explicit_host_and_encoded_repo(self):
        result = operations.ci_status(Context("ci.status"), {"host": "git.example", "repo": "team/project", "ref": "main"})
        self.assertEqual("git.example", result["target"]["host"])
        self.assertEqual(42, result["summary"]["latest"]["id"])

    def test_runbook_rejects_secret_keys_and_unknown_operations(self):
        base = {"schema": "agent-ops/runbook-v1", "name": "demo", "steps": [{"id": "one", "operation": "network.dns", "args": {"host": "example.test"}}]}
        self.assertIs(validate(base), base)
        secret = json.loads(json.dumps(base)); secret["steps"][0]["args"]["token"] = "x"
        with self.assertRaises(OpsError):
            validate(secret)
        unknown = json.loads(json.dumps(base)); unknown["steps"][0]["operation"] = "shell.run"
        with self.assertRaises(OpsError):
            validate(unknown)

    def test_runbook_substitution_is_whole_value_only(self):
        doc = {"schema": "agent-ops/runbook-v1", "name": "demo", "parameters": {"host": "example.test"}, "steps": [{"id": "one", "operation": "network.dns", "args": {"host": "https://${host}"}}]}
        with self.assertRaises(OpsError):
            validate(doc)

    def test_compact_outputs_reduce_repeated_raw_payload(self):
        operations.eks_refresh(Context("eks.refresh"), {"target": "demo", "aws_profile": "kion-prod", "region": "us-east-1", "cluster": "prod-use1"})
        cases = [
            operations.k8s_health(Context("k8s.health"), {"target": "demo", "namespace": "platform"}),
            operations.argo_app(Context("argo.app"), {"argocd_context": "central", "app": "demo"}),
            operations.container_status(Context("container.status"), {"engine": "docker", "name": "web"}),
            operations.ci_status(Context("ci.status"), {"host": "git.example", "repo": "team/project"}),
        ]
        raw = ("unused-field: verbose diagnostic context\n" * 500)
        for result in cases:
            compact = json.dumps(result, separators=(",", ":"))
            self.assertLess(len(compact), len(raw))
            self.assertLess(len(compact.splitlines()), len(raw.splitlines()))


if __name__ == "__main__":
    unittest.main()
