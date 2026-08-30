from dataclasses import dataclass


@dataclass(frozen=True)
class Operation:
    name: str
    handler: object
    required: tuple = ()
    allowed: tuple = ()
    executables: tuple = ()
    runbook: bool = True
    mutation: str = "none"
    timeout: int = 20


REGISTRY = {}


def operation(name, *, required=(), allowed=(), executables=(), runbook=True, mutation="none", timeout=20):
    def decorate(func):
        accepted = tuple(dict.fromkeys((*required, *allowed)))
        REGISTRY[name] = Operation(name, func, tuple(required), accepted, tuple(executables), runbook, mutation, timeout)
        return func
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
