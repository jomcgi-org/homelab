import hashlib
import inspect
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class VerifyResult:
    passed: bool
    feedback: str
    # Graded verifiers (e.g. mutation) report a 0..1 score alongside the pass/fail
    # gate so partial credit is recorded; binary verifiers leave it None.
    score: float | None = None

    def __post_init__(self) -> None:
        if self.score is not None and not (
            math.isfinite(self.score) and 0.0 <= self.score <= 1.0
        ):
            raise ValueError(
                f"score must be None or finite in [0, 1], got {self.score!r}"
            )


_REGISTRY: dict[str, Callable[[Path, dict], VerifyResult]] = {}


def register(kind: str):
    def deco(fn):
        _REGISTRY[kind] = fn
        return fn

    return deco


def get_verifier(kind: str) -> Callable[[Path, dict], VerifyResult]:
    if kind not in _REGISTRY:
        raise KeyError(f"unknown verifier kind: {kind}")
    return _REGISTRY[kind]


def verifier_source_hash(kind: str) -> str:
    """Short hash of the source module implementing `kind`, so editing verifier
    code invalidates its cached cells."""
    fn = get_verifier(kind)
    module = inspect.getmodule(fn)
    src = inspect.getsource(module) if module is not None else ""
    return hashlib.sha256(src.encode()).hexdigest()[:8]


# import submodules so their @register runs
from . import (  # noqa: E402,F401
    checks,
    command,
    compile,
    helm,
    jsonmatch,
    lint,
    mutation,
    pytest,
    rbac,
    review,
    speedup,
)
