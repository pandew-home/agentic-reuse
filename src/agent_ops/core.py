import json
import os
import re
import selectors
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
    re.compile(r'(?i)(["\'](?:authorization|password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|credential)["\']\s*:\s*)(["\'])(?:\\.|(?!\2).)*\2'),
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
    dropped_bytes: int = 0
    captured_bytes: int = 0
    deadline: float | None = None

    def success(self, *, status="healthy", target=None, summary=None, findings=None, data=None):
        return envelope(
            self, True, status, target or {}, summary or {}, findings or [], data or {}
        )

    def remaining(self, requested: float) -> float:
        if self.deadline is None:
            return requested
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise OpsError("command_timeout", "Diagnostic operation exceeded its deadline", 5)
        return min(requested, remaining)


def redact_text(value: str, limit=MAX_STRING) -> str:
    value = ANSI_RE.sub("", str(value))
    value = TEXT_SECRET_RES[0].sub(r'\1\2[REDACTED]\2', value)
    value = TEXT_SECRET_RES[1].sub(r"\1[REDACTED]", value)
    value = TEXT_SECRET_RES[2].sub(r"\1[REDACTED]", value)
    value = TEXT_SECRET_RES[3].sub("[REDACTED]", value)
    value = TEXT_SECRET_RES[4].sub("[REDACTED PRIVATE KEY]", value)
    value = TEXT_SECRET_RES[5].sub(r"\1[REDACTED]@", value)
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
            "dropped_bytes": ctx.dropped_bytes,
            "captured_bytes": ctx.captured_bytes,
            "ingested_bytes": ctx.captured_bytes + ctx.dropped_bytes,
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


def _stop_process_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(proc.pid, 0)
    except ProcessLookupError:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait()


def _read_bounded(ctx, proc, timeout):
    deadline = time.monotonic() + ctx.remaining(timeout)
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    selector = selectors.DefaultSelector()
    for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, name)
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(proc.args, timeout)
            events = selector.select(remaining)
            if not events and proc.poll() is None:
                continue
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffer = buffers[key.data]
                available = max(0, MAX_CAPTURE - len(buffer))
                retained = min(len(chunk), available)
                buffer.extend(chunk[:retained])
                dropped = len(chunk) - retained
                if dropped:
                    ctx.truncated = True
                    ctx.dropped_bytes += dropped
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(proc.args, timeout)
        proc.wait(timeout=remaining)
        return bytes(buffers["stdout"]), bytes(buffers["stderr"])
    finally:
        selector.close()


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
        if input_text is not None:
            proc.stdin.write(input_text.encode())
            proc.stdin.close()
        out, err = _read_bounded(ctx, proc, timeout)
        ctx.captured_bytes += len(out) + len(err)
    except subprocess.TimeoutExpired:
        _stop_process_group(proc)
        raise OpsError("command_timeout", f"Diagnostic command exceeded {timeout}s", 5, {"executable": argv[0]})
    finally:
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None and not stream.closed:
                stream.close()
    stdout = out.decode("utf-8", "replace")
    if redact_stdout:
        stdout = redact_text(stdout, None)
    stderr = redact_text(err.decode("utf-8", "replace"), None)
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


def dump(result, *, quiet=False):
    if quiet:
        result = {key: value for key, value in result.items() if key not in ("meta", "operation")}
    if "meta" in result:
        result["meta"]["envelope_bytes"] = len(json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return json.dumps(result, sort_keys=True, separators=(",", ":"))
