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
from agent_ops.core import MAX_CAPTURE, Context, OpsError, error_envelope, redact_text, run, run_json, sanitize
from agent_ops.registry import REGISTRY, Operation
from agent_ops.runbooks import execute, validate


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
if mode == "noisy":
    sys.stdout.write("x" * 2100000)
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
        for name in ("docker", "argocd", "glab", "aws", "kubectl", "fake", "fake-auth", "fake-malformed", "fake-noisy", "fake-timeout"):
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

    def test_json_formatted_secrets_are_redacted(self):
        cleaned = redact_text('{"token":"synthetic-token","password": "synthetic-password","safe":"value"}')
        self.assertNotIn("synthetic-token", cleaned)
        self.assertNotIn("synthetic-password", cleaned)
        self.assertIn('"safe":"value"', cleaned)

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

    def test_subprocess_capture_is_bounded_while_reading(self):
        ctx = Context("test")
        output, _, _ = run(ctx, ["fake-noisy"])
        self.assertEqual(MAX_CAPTURE, len(output))
        self.assertTrue(ctx.truncated)
        self.assertEqual(0, ctx.omitted)
        self.assertGreater(ctx.dropped_bytes, 0)

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

    def test_argo_operation_omits_helm_parameter_values(self):
        app = {
            "metadata": {"name": "demo"},
            "spec": {"destination": {}},
            "status": {
                "health": {"status": "Degraded"},
                "sync": {"status": "OutOfSync"},
                "operationState": {
                    "phase": "Failed",
                    "syncResult": {
                        "revision": "abc123",
                        "source": {
                            "repoURL": "https://git.example/team/repo",
                            "path": "chart",
                            "helm": {"parameters": [{"name": "db.password", "value": "synthetic-secret"}]},
                        },
                    },
                },
            },
        }
        _, details, _, _ = operations._argo_summary(app, Context("argo.app"))
        encoded = json.dumps(details)
        self.assertNotIn("synthetic-secret", encoded)
        self.assertEqual("https://git.example/team/repo", details["operation"]["sync_result"]["source"]["repoURL"])

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

    def test_ci_failure_requires_numeric_pipeline(self):
        with self.assertRaises(OpsError) as caught:
            operations.ci_failures(Context("ci.failures"), {"host": "git.example", "repo": "team/project", "pipeline": "1/../../tokens"})
        self.assertEqual("invalid_target", caught.exception.code)

    def test_runbook_rejects_secret_keys_and_unknown_operations(self):
        base = {"schema": "agent-ops/runbook-v1", "name": "demo", "steps": [{"id": "one", "operation": "network.dns", "args": {"host": "example.test"}}]}
        self.assertIs(validate(base), base)
        secret = json.loads(json.dumps(base)); secret["steps"][0]["args"]["token"] = "x"
        with self.assertRaises(OpsError):
            validate(secret)
        unknown = json.loads(json.dumps(base)); unknown["steps"][0]["operation"] = "shell.run"
        with self.assertRaises(OpsError):
            validate(unknown)
        authorization = json.loads(json.dumps(base)); authorization["parameters"] = {"authorization": "Bearer synthetic"}
        with self.assertRaises(OpsError):
            validate(authorization)

    def test_runbook_rejects_invalid_choices_and_sensitive_operations(self):
        invalid = {"schema": "agent-ops/runbook-v1", "name": "demo", "steps": [{"id": "one", "operation": "network.dns", "args": {"host": "example.test", "type": "BOGUS"}}]}
        with self.assertRaises(OpsError):
            validate(invalid)
        for operation, args in (
            ("k8s.logs", {"target": "demo", "namespace": "platform", "pod": "web"}),
            ("network.http", {"url": "https://example.test"}),
            ("eks.refresh", {"target": "demo", "aws_profile": "p", "region": "r", "cluster": "c"}),
        ):
            doc = {"schema": "agent-ops/runbook-v1", "name": "demo", "steps": [{"id": "one", "operation": operation, "args": args}]}
            with self.assertRaises(OpsError):
                validate(doc)

    def test_runbook_substitution_is_whole_value_only(self):
        doc = {"schema": "agent-ops/runbook-v1", "name": "demo", "parameters": {"host": "example.test"}, "steps": [{"id": "one", "operation": "network.dns", "args": {"host": "https://${host}"}}]}
        with self.assertRaises(OpsError):
            validate(doc)

    def test_runbook_revalidates_supplied_parameter_values(self):
        doc = {"schema": "agent-ops/runbook-v1", "name": "demo", "parameters": {"record_type": "A"}, "steps": [{"id": "one", "operation": "network.dns", "args": {"host": "example.test", "type": "${record_type}"}}]}
        with self.assertRaises(OpsError):
            execute(Context("run"), doc, {"record_type": "BOGUS"})

    def test_runbook_uses_concurrency_and_compact_step_results(self):
        def slow(ctx, args):
            time.sleep(0.1)
            return ctx.success(summary={"value": args["value"]}, data={"verbose": "x" * 1000})

        operation = Operation("test.slow", slow, ("value",), ("value",), (), True, "none", 20)
        doc = {"schema": "agent-ops/runbook-v1", "name": "demo", "concurrency": 4, "steps": [{"id": name, "operation": "test.slow", "args": {"value": name}} for name in ("one", "two", "three", "four")]}
        with mock.patch.dict(REGISTRY, {"test.slow": operation}):
            started = time.monotonic()
            result = execute(Context("run"), doc)
        self.assertLess(time.monotonic() - started, 0.3)
        self.assertEqual(["one", "two", "three", "four"], [step["id"] for step in result["data"]["steps"]])
        self.assertNotIn("data", result["data"]["steps"][0])

    def test_optional_metrics_do_not_degrade_healthy_cluster(self):
        documents = iter([{"items": []}] * 5)
        with mock.patch("agent_ops.operations.ensure_target", return_value=("demo", pathlib.Path("/tmp/demo"))), mock.patch("agent_ops.operations._kubectl_json", side_effect=lambda *args, **kwargs: next(documents)), mock.patch("agent_ops.operations.run_json", side_effect=OpsError("command_failed", "metrics unavailable")):
            result = operations.k8s_health(Context("k8s.health"), {"target": "demo", "namespace": "platform"})
        self.assertEqual("healthy", result["status"])
        self.assertEqual("info", result["findings"][0]["severity"])

    def test_failed_job_is_critical(self):
        job = {"metadata": {"name": "batch"}, "spec": {}, "status": {"failed": 1, "conditions": [{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}]}}
        documents = iter([job, {"items": []}])
        with mock.patch("agent_ops.operations.ensure_target", return_value=("demo", pathlib.Path("/tmp/demo"))), mock.patch("agent_ops.operations._kubectl_json", side_effect=lambda *args, **kwargs: next(documents)):
            result = operations.k8s_workload(Context("k8s.workload"), {"target": "demo", "namespace": "platform", "kind": "job", "name": "batch"})
        self.assertEqual("critical", result["status"])
        self.assertEqual("job_failed", result["findings"][0]["code"])

    def test_eks_identity_change_forces_refresh(self):
        target = {"id": "demo", "aws_profile": "new-profile", "aws_region": "us-east-1", "eks_cluster": "new-cluster"}
        cached = {"stale": False, "aws_profile": "old-profile", "aws_region": "us-east-1", "eks_cluster": "old-cluster"}
        with mock.patch("agent_ops.operations.eks_status_data", return_value=cached), mock.patch("agent_ops.operations.eks_refresh") as refresh:
            operations._refresh_if_needed(Context("gitops.target"), target)
        refresh.assert_called_once()

    def test_multicluster_propagates_truncation_metadata(self):
        child = {"id": "demo", "ok": True, "status": "healthy", "commands_run": 1, "truncated": True, "omitted": 7, "dropped_bytes": 99, "kubernetes": {}, "apps": [], "findings": []}
        with mock.patch("agent_ops.operations._gitops_target", side_effect=lambda *args: dict(child)):
            result = operations.gitops_multicluster(Context("gitops.multicluster"), {"targets": [{"id": "demo"}]})
        self.assertTrue(result["meta"]["truncated"])
        self.assertEqual(7, result["meta"]["omitted"])
        self.assertEqual(99, result["meta"]["dropped_bytes"])

    def test_http_rejects_private_targets(self):
        with mock.patch("agent_ops.operations.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("169.254.169.254", 0))]):
            with self.assertRaises(OpsError) as caught:
                operations.network_http(Context("network.http"), {"url": "http://metadata.example/"})
        self.assertEqual("unsafe_url", caught.exception.code)

    def test_http_connects_to_validated_address(self):
        response = mock.Mock(status=200)
        response.read.return_value = b""
        response.getheaders.return_value = [("Content-Type", "text/plain")]
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with mock.patch("agent_ops.operations._resolve_public_http_host", return_value="203.0.113.10"), mock.patch("agent_ops.operations.http.client.HTTPConnection", return_value=connection) as factory:
            result = operations.network_http(Context("network.http"), {"url": "http://example.com/status?full=1"})
        factory.assert_called_once_with("203.0.113.10", port=80, timeout=10)
        connection.request.assert_called_once_with("HEAD", "/status?full=1", headers={"Host": "example.com", "User-Agent": "agent-ops/1"})
        self.assertEqual(200, result["summary"]["status"])

    def test_compact_outputs_fit_upstream_shaped_budgets(self):
        operations.eks_refresh(Context("eks.refresh"), {"target": "demo", "aws_profile": "kion-prod", "region": "us-east-1", "cluster": "prod-use1"})
        cases = {
            "k8s": (operations.k8s_health(Context("k8s.health"), {"target": "demo", "namespace": "platform"}), json.dumps({"items": [{"metadata": {"name": f"pod-{index}", "labels": {"app": "web"}}, "spec": {"containers": [{"name": "web", "image": "example/web:1"}]}, "status": {"phase": "Running", "containerStatuses": [{"restartCount": 0}]}} for index in range(100)]})),
            "argo": (operations.argo_app(Context("argo.app"), {"argocd_context": "central", "app": "demo"}), json.dumps({"status": {"resources": [{"group": "apps", "kind": "Deployment", "name": f"app-{index}", "status": "Synced", "health": {"status": "Healthy"}} for index in range(100)]}})),
            "container": (operations.container_status(Context("container.status"), {"engine": "docker", "name": "web"}), json.dumps([{"Config": {"Env": [f"SETTING_{index}=value" for index in range(200)]}, "Mounts": [{"Source": f"/source/{index}", "Destination": f"/target/{index}"} for index in range(100)]}])),
            "gitlab": (operations.ci_status(Context("ci.status"), {"host": "git.example", "repo": "team/project"}), json.dumps([{"id": index, "status": "success", "ref": "main", "sha": "a" * 40, "web_url": f"https://git.example/team/project/-/pipelines/{index}", "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:01:00Z", "user": {"name": "Synthetic User"}} for index in range(100)])),
        }
        for name, (result, raw) in cases.items():
            compact = json.dumps(result, separators=(",", ":"))
            self.assertLess(len(compact), len(raw) * 0.25, name)
            self.assertLess(len(compact), 5000, name)


if __name__ == "__main__":
    unittest.main()
