import json
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

SCHEMA = "agent-ops/v1"
MAX_STRING = 2000
MAX_CAPTURE = 2_000_000
ANSI_RE = re.compile(r"\x1b(?:[@-_][0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
SECRET_KEY_RE = re.compile(r"(?i)(authorization|password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential)")
TEXT_SECRET_RES = (
    re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?\S+"),
    re.compile(r"(?i)((?:password|passwd|secret|token|api[_-]?key|access[_-]?key|credential)\s*[:=]\s*)\S+"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?s)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----"),
    re.compile(r"(?i)(https?://)([^/@\s:]+):([^/@\s]+)@"),
)


class OpsError(Exception):
    def __init__(self, code: str, message: str, exit_code: int = 5, details: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.details = details


@dataclass
class Context:
    operation: str
    started: float = field(default_factory=time.monotonic)
    commands_run: int = 0
    truncated: bool = False
    omitted: int = 0

    def success(self, *, status="healthy", target=None, summary=None, findings=None, data=None):
        return envelope(
            self, True, status, target or {}, summary or {}, findings or [], data or {}
        )


def redact_text(value: str, limit=MAX_STRING) -> str:
    value = ANSI_RE.sub("", str(value))
    value = TEXT_SECRET_RES[0].sub(r"\1[REDACTED]", value)
    value = TEXT_SECRET_RES[1].sub(r"\1[REDACTED]", value)
    value = TEXT_SECRET_RES[2].sub("[REDACTED]", value)
    value = TEXT_SECRET_RES[3].sub("[REDACTED PRIVATE KEY]", value)
    value = TEXT_SECRET_RES[4].sub(r"\1[REDACTED]@", value)
    return value[:limit] if limit is not None else value


def sanitize(value: Any, key="") -> Any:
    if SECRET_KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): sanitize(v, str(k)) for k, v in sorted(value.items(), key=lambda x: str(x[0]))}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def finding(severity, code, message, resource="", evidence=None):
    return sanitize({
        "severity": severity,
        "code": code,
        "message": message,
        "resource": resource,
        "evidence": evidence or {},
    })


def envelope(ctx, ok, status, target, summary, findings, data, error=None):
    result = {
        "schema": SCHEMA,
        "ok": ok,
        "status": status,
        "operation": ctx.operation,
        "target": sanitize(target),
        "summary": sanitize(summary),
        "findings": sanitize(findings),
        "data": sanitize(data),
        "meta": {
            "duration_ms": int((time.monotonic() - ctx.started) * 1000),
            "commands_run": ctx.commands_run,
            "truncated": ctx.truncated,
            "omitted": ctx.omitted,
        },
    }
    if error is not None:
        result["error"] = sanitize(error)
    return result


def error_envelope(ctx: Context, exc: OpsError):
    return envelope(ctx, False, "error", {}, {}, [], {}, {
        "code": exc.code,
        "message": exc.message,
        "details": exc.details or {},
    })


def narrow_env():
    keep = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE", "DOCKER_HOST", "CONTAINER_HOST")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.update({"NO_COLOR": "1", "CLICOLOR": "0", "PAGER": "cat", "GIT_PAGER": "cat", "AWS_PAGER": "", "CI": "1"})
    return env


def run(ctx: Context, argv, timeout=20, input_text=None, allowed_codes=(0,), redact_stdout=True):
    ctx.commands_run += 1
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            shell=False,
            start_new_session=True,
            env=narrow_env(),
        )
    except FileNotFoundError:
        raise OpsError("dependency_missing", f"Required executable is not installed: {argv[0]}", 4)
    try:
        out, err = proc.communicate(input_text.encode() if input_text is not None else None, timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
        raise OpsError("command_timeout", f"Diagnostic command exceeded {timeout}s", 5, {"executable": argv[0]})
    if len(out) > MAX_CAPTURE or len(err) > MAX_CAPTURE:
        ctx.truncated = True
        out, err = out[:MAX_CAPTURE], err[:MAX_CAPTURE]
    stdout = out.decode("utf-8", "replace")
    if redact_stdout:
        stdout = redact_text(stdout, None)
    stderr = redact_text(err.decode("utf-8", "replace"))
    if proc.returncode not in allowed_codes:
        code = "authentication_failed" if re.search(r"(?i)(authorization|unauthorized|forbidden|credential|login|authentication|access denied)", stderr) else "command_failed"
        raise OpsError(code, stderr.strip() or f"{argv[0]} exited with {proc.returncode}", 4 if code == "authentication_failed" else 5, {"exit_code": proc.returncode, "executable": argv[0]})
    return stdout, stderr, proc.returncode


def run_json(ctx, argv, timeout=20):
    stdout, _, _ = run(ctx, argv, timeout, redact_stdout=False)
    try:
        return json.loads(stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise OpsError("malformed_output", "Upstream command returned malformed JSON", 6, {"reason": str(exc)})


def bounded(items, ctx: Context, limit=50):
    values = list(items)
    if len(values) > limit:
        ctx.truncated = True
        ctx.omitted += len(values) - limit
    return values[:limit]


def dump(result):
    return json.dumps(sanitize(result), sort_keys=True, separators=(",", ":"))
