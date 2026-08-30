from dataclasses import dataclass
from functools import wraps
import time


@dataclass(frozen=True)
class Operation:
    name: str
    handler: object
    required: tuple = ()
    allowed: tuple = ()
    executables: tuple = ()
    runbook: bool = False
    mutation: str = "none"
    timeout: int = 20


REGISTRY = {}


def operation(name, *, required=(), allowed=(), executables=(), runbook=False, mutation="none", timeout=20):
    def decorate(func):
        @wraps(func)
        def with_deadline(ctx, args):
            previous = ctx.deadline
            operation_deadline = time.monotonic() + timeout
            ctx.deadline = min(previous, operation_deadline) if previous is not None else operation_deadline
            try:
                return func(ctx, args)
            finally:
                ctx.deadline = previous

        accepted = tuple(dict.fromkeys((*required, *allowed)))
        REGISTRY[name] = Operation(name, with_deadline, tuple(required), accepted, tuple(executables), runbook, mutation, timeout)
        return with_deadline
    return decorate


def describe():
    return [
        {
            "name": op.name,
            "required": list(op.required),
            "allowed": list(op.allowed),
            "executables": list(op.executables),
            "runbook": op.runbook,
            "mutation": op.mutation,
            "timeout_seconds": op.timeout,
        }
        for op in sorted(REGISTRY.values(), key=lambda x: x.name)
    ]
