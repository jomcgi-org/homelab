import hashlib
import inspect
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Literal


@dataclass
class PerformanceSample:
    baseline_s: float
    candidate_s: float
    order: Literal["baseline-first", "candidate-first"]

    def __post_init__(self) -> None:
        for value in (self.baseline_s, self.candidate_s):
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError("samples must be finite positive numbers")
        if self.order not in ("baseline-first", "candidate-first"):
            raise ValueError("invalid pair order")

    @classmethod
    def model_validate(cls, value):
        if isinstance(value, cls):
            return value
        if not isinstance(value, dict):
            raise TypeError("sample must be an object")
        return cls(**value)


@dataclass
class PerformanceRecord:
    correctness: bool | None
    score: float
    pass_threshold: float
    pair_count: int
    fixture_version: str
    metric: Literal["wall_clock_median_paired_ratio"] = "wall_clock_median_paired_ratio"
    warmup: list[PerformanceSample] = field(default_factory=list)
    samples: list[PerformanceSample] = field(default_factory=list)
    ratios: list[float] = field(default_factory=list)
    median_ratio: float | None = None
    highest_bucket: float | None = None

    def __post_init__(self) -> None:
        self.warmup = [PerformanceSample.model_validate(s) for s in self.warmup]
        self.samples = [PerformanceSample.model_validate(s) for s in self.samples]
        if self.correctness is not None and type(self.correctness) is not bool:
            raise ValueError("correctness must be bool or None")
        if self.metric != "wall_clock_median_paired_ratio":
            raise ValueError("invalid performance metric")
        if type(self.pair_count) is not int or self.pair_count <= 0:
            raise ValueError("pair_count must be positive")
        if not self.fixture_version:
            raise ValueError("fixture_version is required")
        for value in (self.score, self.pass_threshold):
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError("score and threshold must be finite in [0, 1]")
        for value in [*self.ratios, self.median_ratio, self.highest_bucket]:
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ValueError("ratios and bucket must be finite positive values")

    def model_dump(self) -> dict:
        return asdict(self)


@dataclass
class VerifyResult:
    passed: bool
    feedback: str
    # Graded verifiers (e.g. mutation) report a 0..1 score alongside the pass/fail
    # gate so partial credit is recorded; binary verifiers leave it None.
    score: float | None = None
    performance: PerformanceRecord | None = None

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
    for dependency in getattr(fn, "source_dependencies", ()):
        src += inspect.getsource(dependency)
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
