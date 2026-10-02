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


def accepts_final_response(verify) -> bool:
    """A declared keyword-only parameter opts a verifier into completion grading."""
    parameter = inspect.signature(verify).parameters.get("final_response")
    return parameter is not None and parameter.kind == inspect.Parameter.KEYWORD_ONLY


def final_response_text(value) -> str:
    """Keep only harness-captured text; missing or invalid content is empty."""
    return value.strip() if isinstance(value, str) else ""


def grade_agent(verify, workdir: Path, args: dict, final_response) -> VerifyResult:
    if accepts_final_response(verify):
        return verify(workdir, args, final_response=final_response_text(final_response))
    return verify(workdir, args)


def verifier_source_hash(kind: str) -> str:
    """Short hash of the source module implementing `kind`, so editing verifier
    code invalidates its cached cells."""
    fn = get_verifier(kind)
    module = inspect.getmodule(fn)
    src = inspect.getsource(module) if module is not None else ""
    return hashlib.sha256(src.encode()).hexdigest()[:8]


# import submodules so their @register runs
from . import (  # noqa: E402,F401
    command,
    compile,
    decision_conflict,
    helm,
    jsonmatch,
    lint,
    pytest,
    rbac,
)
