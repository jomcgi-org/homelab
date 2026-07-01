import hashlib
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class VerifyResult:
    passed: bool
    feedback: str


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
from . import command, helm, compile, lint, rbac, jsonmatch  # noqa: E402,F401
