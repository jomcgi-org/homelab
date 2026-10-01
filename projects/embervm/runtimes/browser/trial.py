"""Offline MCP/CLI comparison. Selection requires complete, blind run records.

Adapters normalize input_tokens to inclusive input: uncached input plus cache
reads and writes. Cache buckets are disjoint. Reasoning uses the output price
and is added only if the provider's output count excludes it. Prices are caller
supplied USD per million, never fetched. Image retrieval and verification must
precede selection; a complete EvidenceVerdict is the adapter's verified result.
"""

import hashlib
import json
import math
import random
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from statistics import median

from evidence import (
    DEFAULT_VIEWPORTS,
    EvidenceRecord,
    EvidenceVerdict,
    preview_url,
    utc_time,
)

ARMS = ("mcp", "cli")
SCENARIOS = ("layout", "interaction", "diagnostics", "review")
SCENARIO_BRIEFS = {
    "layout": "Inspect and correct layout at 1440x900 and 390x844.",
    "interaction": "Reproduce an interaction defect and verify its correction.",
    "diagnostics": "Inspect console and network failures.",
    "review": "Review the rendered implementation against its original brief.",
}
SELECTION_RULE = (
    ("accepted_completion_rate", "higher"),
    ("missed_defects_plus_regressions", "lower"),
    ("correction_rounds", "lower"),
    ("human_interventions_post_start", "lower"),
    ("list_cost_usd", "lower"),
    ("median_elapsed_monotonic_s", "lower"),
)
RELATIVE_TOLERANCE = Decimal("0.05")
TOKEN_CLASSES = (
    "input_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "output_tokens",
)
COUNT_FIELDS = (
    "seeded_defects_total",
    "missed_defects",
    "introduced_regressions",
    "correction_rounds",
    "human_interventions_post_start",
    "browser_tool_payload_bytes",
    "browser_payload_bytes",
    "tool_payload_bytes",
)


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(name + " must be a nonempty string")


def _count(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(name + " must be a nonnegative integer")


def _duration(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(name + " must be a finite nonnegative duration")


@dataclass(frozen=True)
class TrialSpec:
    app_commit: str
    source_url: str
    brief_sha256: str
    model: str
    effort: str
    output_contract: str
    runs_per_arm: int = 3
    seed: int = 0

    def __post_init__(self):
        for name, width in (("app_commit", 40), ("brief_sha256", 64)):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or re.fullmatch(r"[0-9a-f]{" + str(width) + r"}", value) is None
            ):
                raise ValueError(name + " must be lowercase hex of width " + str(width))
        if not isinstance(self.source_url, str) or not preview_url(self.source_url):
            raise ValueError("source_url must pass evidence.preview_url")
        # urlsplit validates numeric ports lazily, so check the effective port too.
        from grants import origin

        origin(self.source_url)
        for name in ("model", "effort", "output_contract"):
            _text(getattr(self, name), name)
        _count(self.runs_per_arm, "runs_per_arm")
        if self.runs_per_arm == 0:
            raise ValueError("runs_per_arm must be positive")
        if type(self.seed) is not int or self.seed < 0:
            raise ValueError("seed must be a nonnegative integer")


@dataclass(frozen=True)
class PlannedRun:
    arm: str
    scenario: str
    repetition: int
    run_id: str
    session_id: str
    profile_key: str
    spec: TrialSpec


def schedule(spec: TrialSpec) -> list[PlannedRun]:
    """Deterministic reservation identities; actual worker IDs echo in records.

    Each logical slot gets a distinct session reservation and profile key.
    Worker adapters create fresh physical profiles and record their actual IDs.
    Both arms receive the same immutable spec and original brief.
    """
    if not isinstance(spec, TrialSpec):
        raise ValueError("TrialSpec is required")
    spec.__post_init__()
    fingerprint = hashlib.sha256(
        json.dumps(asdict(spec), sort_keys=True).encode()
    ).hexdigest()
    rng, planned = random.Random(spec.seed), []
    for scenario in SCENARIOS:
        for repetition in range(spec.runs_per_arm):
            arms = list(ARMS)
            rng.shuffle(arms)
            for arm in arms:
                key = hashlib.sha256(
                    f"{fingerprint}:{scenario}:{repetition}:{arm}".encode()
                ).hexdigest()
                planned.append(
                    PlannedRun(
                        arm,
                        scenario,
                        repetition,
                        "run-" + key,
                        "session-" + key,
                        "profile-" + key,
                        spec,
                    )
                )
    return planned


@dataclass(frozen=True)
class UsageRecord:
    request_id: str
    model: str
    phase: str
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    reasoning_tokens: int
    reasoning_included_in_output: bool

    def __post_init__(self):
        _text(self.request_id, "request_id")
        _text(self.model, "model")
        if self.phase not in ("implementation", "review"):
            raise ValueError("usage phase must be implementation or review")
        for name in TOKEN_CLASSES + ("reasoning_tokens",):
            _count(getattr(self, name), name)
        if type(self.reasoning_included_in_output) is not bool:
            raise ValueError("reasoning inclusion flag must be boolean")
        if self.cache_read_tokens + self.cache_write_tokens > self.input_tokens:
            raise ValueError("cache counts exceed inclusive input")
        if (
            self.reasoning_included_in_output
            and self.reasoning_tokens > self.output_tokens
        ):
            raise ValueError("included reasoning exceeds output")


@dataclass(frozen=True)
class UsageTotals:
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    list_cost_usd: Decimal


def account_usage(records, price_table) -> UsageTotals:
    """Sum disjoint billable classes. Refuse duplicates and missing prices."""
    if not isinstance(records, (tuple, list)) or not records:
        raise ValueError("nonempty usage records are required")
    if not isinstance(price_table, dict) or not price_table:
        raise ValueError("caller-supplied price table is required")
    counts = {name: 0 for name in TOKEN_CLASSES}
    ids, cost = set(), Decimal(0)
    for record in records:
        if not isinstance(record, UsageRecord):
            raise ValueError("invalid usage record")
        record.__post_init__()
        if record.request_id in ids:
            raise ValueError("duplicate request_id")
        ids.add(record.request_id)
        prices = price_table.get(record.model)
        if not isinstance(prices, dict) or set(prices) != set(TOKEN_CLASSES):
            raise ValueError("missing or incomplete prices for model " + record.model)
        billable = {
            "input_tokens": record.input_tokens
            - record.cache_read_tokens
            - record.cache_write_tokens,
            "cache_read_tokens": record.cache_read_tokens,
            "cache_write_tokens": record.cache_write_tokens,
            "output_tokens": record.output_tokens
            + (0 if record.reasoning_included_in_output else record.reasoning_tokens),
        }
        for name, count in billable.items():
            rate = prices[name]
            if type(rate) not in (int, float, Decimal):
                raise ValueError("price must be a finite nonnegative number")
            rate = Decimal(str(rate))
            if not rate.is_finite() or rate < 0:
                raise ValueError("price must be a finite nonnegative number")
            counts[name] += count
            cost += Decimal(count) * rate / Decimal(1_000_000)
    return UsageTotals(**counts, list_cost_usd=cost)


@dataclass(frozen=True)
class RunRecord:
    arm: str
    scenario: str
    repetition: int
    run_id: str
    session_id: str
    profile_key: str
    implementer_session_id: str
    reviewer_session_id: str
    reviewer_blind: bool
    spec: TrialSpec
    accepted: bool
    seeded_defects_total: int
    missed_defects: int
    introduced_regressions: int
    functional_pass: dict[tuple[int, int], bool]
    accessibility_pass: dict[tuple[int, int], bool]
    visual_pass: dict[tuple[int, int], bool]
    correction_rounds: int
    human_interventions_post_start: int
    elapsed_monotonic_s: float
    cold_start_s: float
    usage: tuple[UsageRecord, ...]
    browser_tool_payload_bytes: int
    browser_payload_bytes: int
    tool_payload_bytes: int
    evidence_verdict: EvidenceVerdict
    evidence_records: tuple[EvidenceRecord, ...]


@dataclass(frozen=True)
class ArmMetrics:
    accepted_completion_rate: Decimal
    missed_defects_plus_regressions: int
    correction_rounds: int
    human_interventions_post_start: int
    list_cost_usd: Decimal
    median_elapsed_monotonic_s: Decimal
    browser_payload_bytes: int
    tool_payload_bytes: int


@dataclass(frozen=True)
class TrialResult:
    status: str
    reasons: tuple[str, ...]
    selected_arm: str | None = None
    scenario_routes: dict[str, str] = field(default_factory=dict)
    scenario_winners: dict[str, str | None] = field(default_factory=dict)
    overall_winner: str | None = None
    metrics: dict[str, dict[str, ArmMetrics]] = field(default_factory=dict)


def _validate_run(run, spec, now):
    if not isinstance(run, RunRecord):
        raise ValueError("invalid run record")
    if (
        not isinstance(run.arm, str)
        or run.arm not in ARMS
        or not isinstance(run.scenario, str)
        or run.scenario not in SCENARIOS
    ):
        raise ValueError("unknown arm or scenario")
    _count(run.repetition, "repetition")
    if run.repetition >= spec.runs_per_arm:
        raise ValueError("repetition outside schedule")
    if not isinstance(run.spec, TrialSpec) or run.spec != spec:
        raise ValueError("run spec echo differs")
    run.spec.__post_init__()
    for name in (
        "run_id",
        "session_id",
        "profile_key",
        "implementer_session_id",
        "reviewer_session_id",
    ):
        _text(getattr(run, name), name)
    if run.reviewer_blind is not True or run.reviewer_session_id in (
        run.implementer_session_id,
        run.session_id,
    ):
        raise ValueError("reviewer must be blind and independent")
    if type(run.accepted) is not bool:
        raise ValueError("accepted verdict must be boolean")
    for name in COUNT_FIELDS:
        _count(getattr(run, name), name)
    if run.missed_defects > run.seeded_defects_total:
        raise ValueError("missed defects exceed seeded defects")
    if (
        run.browser_payload_bytes + run.tool_payload_bytes
        != run.browser_tool_payload_bytes
    ):
        raise ValueError("browser/tool payload total differs")
    for name in ("elapsed_monotonic_s", "cold_start_s"):
        _duration(getattr(run, name), name)
    if run.cold_start_s > run.elapsed_monotonic_s:
        raise ValueError("cold start exceeds elapsed duration")
    for name in ("functional_pass", "accessibility_pass", "visual_pass"):
        statuses = getattr(run, name)
        if (
            not isinstance(statuses, dict)
            or set(statuses) != set(DEFAULT_VIEWPORTS)
            or any(type(v) is not bool for v in statuses.values())
        ):
            raise ValueError(name + " requires explicit booleans at both viewports")
        if run.accepted and not all(statuses.values()):
            raise ValueError("accepted run has failed " + name)
    if (
        not isinstance(run.evidence_verdict, EvidenceVerdict)
        or run.evidence_verdict.status != "complete"
        or run.evidence_verdict.reasons
    ):
        raise ValueError("run evidence is incomplete")
    if not isinstance(run.evidence_records, tuple) or not run.evidence_records:
        raise ValueError("run evidence records are missing")
    screenshots, task_ids, artifact_ids = set(), set(), set()
    for artifact in run.evidence_records:
        if not isinstance(artifact, EvidenceRecord):
            raise ValueError("invalid evidence record")
        artifact.__post_init__()
        if (
            artifact.app_commit,
            artifact.source_url,
            artifact.run_id,
            artifact.session_id,
        ) != (spec.app_commit, spec.source_url, run.run_id, run.session_id):
            raise ValueError("evidence identity differs from run")
        task_ids.add(artifact.task_id)
        identity = (artifact.kind, artifact.viewport, artifact.sha256)
        if identity in artifact_ids:
            raise ValueError("duplicate evidence record")
        artifact_ids.add(identity)
        if (
            artifact.upload_status != "uploaded"
            or utc_time(artifact.expires_at) <= now
            or utc_time(artifact.capture_time) > now
        ):
            raise ValueError("evidence is expired, pending, failed or future-dated")
        if artifact.kind == "screenshot":
            screenshots.add(tuple(artifact.viewport))
    if len(task_ids) != 1:
        raise ValueError("evidence has mixed tasks")
    if not set(DEFAULT_VIEWPORTS) <= screenshots:
        raise ValueError(
            "screenshot evidence required at both viewports; accessibility is not visual"
        )


def _metrics(runs, costs):
    return ArmMetrics(
        Decimal(sum(run.accepted for run in runs)) / Decimal(len(runs)),
        sum(run.missed_defects + run.introduced_regressions for run in runs),
        sum(run.correction_rounds for run in runs),
        sum(run.human_interventions_post_start for run in runs),
        sum((costs[run.run_id] for run in runs), Decimal(0)),
        median(Decimal(str(run.elapsed_monotonic_s)) for run in runs),
        sum(run.browser_payload_bytes for run in runs),
        sum(run.tool_payload_bytes for run in runs),
    )


def _compare(metrics):
    for criterion, direction in SELECTION_RULE:
        left, right = (
            getattr(metrics["mcp"], criterion),
            getattr(metrics["cli"], criterion),
        )
        if left == right:
            continue
        if criterion in ("list_cost_usd", "median_elapsed_monotonic_s"):
            # Symmetric tolerance, relative to the larger value. Exactly 5% ties.
            if abs(left - right) <= max(abs(left), abs(right)) * RELATIVE_TOLERANCE:
                continue
        mcp_wins = left > right if direction == "higher" else left < right
        return ("mcp" if mcp_wins else "cli"), criterion
    return None, "all declared criteria tie"


def select(spec, runs, price_table=None, *, now=None) -> TrialResult:
    """Apply the declared rule by scenario and overall, never default an arm."""
    try:
        planned = schedule(spec)
        if not isinstance(runs, (tuple, list)) or not runs:
            raise ValueError("scheduled runs are missing")
        now = datetime.now(timezone.utc) if now is None else now
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise ValueError("evidence check time must be timezone-aware")
        expected = {(run.scenario, run.repetition, run.arm): run for run in planned}
        slots, sessions, profiles, requests, costs = set(), set(), set(), set(), {}
        seeded_counts, tasks = {}, set()
        for run in runs:
            _validate_run(run, spec, now)
            slot = (run.scenario, run.repetition, run.arm)
            if slot in slots:
                raise ValueError("duplicate scheduled run")
            slots.add(slot)
            if run.run_id != expected[slot].run_id:
                raise ValueError("run_id differs from scheduled slot")
            if run.profile_key in profiles:
                raise ValueError("shared profile key")
            profiles.add(run.profile_key)
            # The browser may be controlled by its own implementer session, but
            # neither identity can recur across runs or be reused for review.
            identities = {
                run.session_id,
                run.implementer_session_id,
                run.reviewer_session_id,
            }
            if identities & sessions:
                raise ValueError("shared session across runs")
            sessions.update(identities)
            tasks.update(artifact.task_id for artifact in run.evidence_records)
            pair = (run.scenario, run.repetition)
            if (
                pair in seeded_counts
                and seeded_counts[pair] != run.seeded_defects_total
            ):
                raise ValueError("seeded defect totals differ between paired arms")
            seeded_counts[pair] = run.seeded_defects_total
            totals = account_usage(run.usage, price_table)
            phases = {usage.phase for usage in run.usage}
            if phases != {"implementation", "review"}:
                raise ValueError("implementation and review usage are both required")
            if any(usage.model != spec.model for usage in run.usage):
                raise ValueError("usage model differs from trial model")
            for usage in run.usage:
                if usage.request_id in requests:
                    raise ValueError("duplicate request_id across trial")
                requests.add(usage.request_id)
            costs[run.run_id] = totals.list_cost_usd
        if slots != set(expected):
            raise ValueError("scheduled runs are missing")
        if len(tasks) != 1:
            raise ValueError("trial evidence has mixed tasks")
    except (ValueError, TypeError, AttributeError, KeyError, OverflowError) as error:
        return TrialResult("incomplete", (str(error),))
    metrics, winners, reasons = {}, {}, []
    for scenario in SCENARIOS + ("overall",):
        metrics[scenario] = {
            arm: _metrics(
                [
                    run
                    for run in runs
                    if run.arm == arm
                    and (scenario == "overall" or run.scenario == scenario)
                ],
                costs,
            )
            for arm in ARMS
        }
        winner, criterion = _compare(metrics[scenario])
        winners[scenario] = winner
        reasons.append(f"{scenario}: {winner or 'tie'} ({criterion})")
    overall = winners.pop("overall")
    decided = {winner for winner in winners.values() if winner is not None}
    if not decided and overall is None:
        return TrialResult(
            "no_selection", tuple(reasons), metrics=metrics, scenario_winners=winners
        )
    if len(decided) <= 1:
        selected = next(iter(decided)) if decided else overall
        return TrialResult(
            "selected",
            tuple(reasons),
            selected_arm=selected,
            overall_winner=overall,
            scenario_winners=winners,
            metrics=metrics,
        )
    if overall is None and any(winner is None for winner in winners.values()):
        reasons.append("a tied scenario has no overall winner to route to")
        return TrialResult(
            "no_selection", tuple(reasons), scenario_winners=winners, metrics=metrics
        )
    routes = {scenario: winner or overall for scenario, winner in winners.items()}
    return TrialResult(
        "conditional_routing",
        tuple(reasons),
        scenario_routes=routes,
        scenario_winners=winners,
        overall_winner=overall,
        metrics=metrics,
    )
