"""Manual production read verification using rollback-only PostgreSQL fixtures.

No fixture enters a persistent schema. The normal status reader uses the same
Session against pg_temp. No admission, executor, provider or notification runs.
"""

from __future__ import annotations

import json
import os
import re
import time
from contextlib import contextmanager
from uuid import UUID, uuid4

from opentelemetry import trace
from sqlalchemy import MetaData, Table, event, text
from sqlalchemy.pool import NullPool
from sqlalchemy.sql import Select, visitors
from sqlmodel import Session, create_engine

from factory.orchestration.github_read_cooldown import (
    GitHubReadCooldown,
    GitHubReadDeferred,
    process_memory_snapshot,
)

FIXTURE_VERSION = "factory-history-v1"
RECEIPTS = 128
STARTS_PER_RECEIPT = 8
WARMUP = 1
SAMPLES = 3
MAX_SECONDS = 30
MAX_READS = 6000
# Measured on PostgreSQL for this fixed fixture: 9 reads/receipt + 13 shared.
# This is a work baseline, never a latency threshold. Improvements may use less.
BASELINE_READS = 1165
LOCK_ID = 731046128
PROCESS_INSTANCE = str(uuid4())
tracer = trace.get_tracer(__name__)


class Inconclusive(RuntimeError):
    """Safety bound or missing evidence, never a regression verdict."""


def _models():
    from factory.orchestration.factory_models import (
        FactoryAudit,
        FactoryClassTier,
        FactoryControl,
        FactoryReceipt,
        FactoryReviewVerdict,
        FactoryStart,
        WorkItem,
        WorkItemEdge,
        WorkItemEvent,
    )
    from factory.orchestration.models import SwarmNodeRun, SwarmTask

    return (
        SwarmTask,
        SwarmNodeRun,
        WorkItem,
        WorkItemEdge,
        WorkItemEvent,
        FactoryClassTier,
        FactoryControl,
        FactoryReceipt,
        FactoryStart,
        FactoryAudit,
        FactoryReviewVerdict,
    )


def _seed(session):
    from factory.orchestration.factory_models import (
        FactoryControl,
        FactoryReceipt,
        FactoryStart,
    )
    from factory.orchestration.models import SwarmTask

    session.add(FactoryControl(id="factory", actor="synthetic", state="disabled"))
    tasks = [
        SwarmTask(id=f"synthetic-{i}", task_text="synthetic", conductor_model="luna")
        for i in range(RECEIPTS)
    ]
    session.add_all(tasks)
    session.flush()
    session.add_all(
        [
            FactoryReceipt(
                repo="synthetic/fixture",
                issue_number=i + 1,
                title="synthetic historical receipt",
                body="synthetic " * 256,
                url="https://synthetic.invalid/fixture",
                actor="synthetic",
                state="succeeded",
                task_id=task.id,
                policy_json=json.dumps(
                    {
                        "task_timeout_seconds": 3600,
                        "max_task_turns_hard": 16,
                        "task_budget_usd": 8,
                    }
                ),
            )
            for i, task in enumerate(tasks)
        ]
        + [
            FactoryStart(
                task_id=task.id,
                start_key=f"synthetic:{j}",
                actor="synthetic",
                model="luna",
                status="succeeded",
                max_cost_usd=1,
                cost_usd=0.25,
            )
            for task in tasks
            for j in range(STARTS_PER_RECEIPT)
        ]
    )
    session.flush()
    session.expunge_all()


@contextmanager
def fixtures(connection):
    """Clone table definitions, never mutate global ORM metadata or real rows."""
    metadata = MetaData()
    for model in _models():
        model.__table__.to_metadata(
            metadata, schema="pg_temp", referred_schema_fn=lambda *_: "pg_temp"
        )
    # Foreign keys retain their original schema names. Translate both their
    # targets and ORM reads on this connection; no session escapes this scope.
    connection = connection.execution_options(schema_translate_map={"swarm": "pg_temp"})
    metadata.create_all(connection, checkfirst=False)
    with Session(bind=connection, autoflush=False) as session:
        _seed(session)
        yield session


def _correct(result):
    rows = result.get("receipts", [])
    return (
        result.get("ok") is True
        and result.get("state") == "disabled"
        and len(rows) == RECEIPTS
        and {row["issue_number"] for row in rows} == set(range(1, RECEIPTS + 1))
        and all(
            row["repo"] == "synthetic/fixture"
            and row["state"] == "succeeded"
            and row["turns_used"] == STARTS_PER_RECEIPT
            and row["committed_cost_usd"] == STARTS_PER_RECEIPT * 0.25
            and row["unresolved_starts"] == 0
            for row in rows
        )
        and not result["active_tasks"]
        and not result["landing_tasks"]
    )


def cooldown():
    """Exercise the shipped pacing class with an isolated clock, without I/O."""
    clock = [0.0]
    guard = GitHubReadCooldown(monotonic=lambda: clock[0], wall_time=lambda: 0)
    guard.observe({"status": 429, "classification": "rate_limited", "retry_after": "5"})
    deferred = 0
    for _ in range(3):
        try:
            guard.check()
        except GitHubReadDeferred:
            deferred += 1
    clock[0] = 6
    guard.check()
    return {"correct": deferred == 3, "deferred": deferred, "external_api_reads": 0}


def cached_reads(identity):
    """Cold and warm reads use the production cache and an isolated fixture store."""
    from factory.orchestration import factory_rollout

    samples = []
    for failing in (False, True):
        cache = {}
        reads = 0
        started = time.monotonic()
        key = ("synthetic", "failure" if failing else "success")

        def read():
            nonlocal reads
            reads += 1
            if failing:
                raise ValueError("synthetic upstream unavailable")
            return {"fixture": FIXTURE_VERSION, "receipts": RECEIPTS}

        for index in range(WARMUP + SAMPLES):
            before = reads
            sample_start = time.monotonic()
            with tracer.start_as_current_span(
                "swarm.synthetic.cache",
                attributes={
                    **identity,
                    "synthetic.scenario": "cache-failure"
                    if failing
                    else "cache-success",
                },
            ) as span:
                try:
                    result = factory_rollout._cached(key, read, cache=cache)
                    correct = not failing and result == {
                        "fixture": FIXTURE_VERSION,
                        "receipts": RECEIPTS,
                    }
                except ValueError as exc:
                    correct = failing and str(exc) == "synthetic upstream unavailable"
                # A process pause spanning expiry is insufficient warm evidence,
                # not a cache regression. Nothing sleeps or modifies its clock.
                if time.monotonic() - started >= factory_rollout.READ_CACHE_TTL_SECONDS:
                    raise Inconclusive("cache samples crossed the reuse window")
                cached = cache.get(key, (None, None))[1]
                if failing:
                    correct = (
                        correct
                        and isinstance(cached, ValueError)
                        and cached.__traceback__ is None
                    )
                sample = {
                    "warmup": index < WARMUP,
                    "correct": correct and reads == 1 and len(cache) == 1,
                    "fixture_reads": reads - before,
                    "cache_hit": reads == before,
                    "cache_entries": len(cache),
                    "external_api_reads": 0,
                    "duration_ms": round((time.monotonic() - sample_start) * 1000, 3),
                }
                span.set_attributes({f"synthetic.{k}": v for k, v in sample.items()})
                samples.append(sample)
                if not sample["correct"]:
                    return samples
    return samples


def measure(session, identity):
    from factory.orchestration.factory_controls import status

    connection = session.connection()
    started = time.monotonic()
    reads = 0
    tables = {m.__table__.name for m in _models()}

    def budget(_conn, _cursor, statement, _params, context, _many):
        nonlocal reads
        if not statement.lstrip().upper().startswith("SELECT "):
            raise Inconclusive("status attempted a non-read statement")
        compiled = context.compiled
        if compiled is None or not isinstance(compiled.statement, Select):
            raise Inconclusive("status attempted an unscoped read")
        referenced = [
            node
            for node in visitors.iterate(compiled.statement)
            if isinstance(node, Table)
        ]
        if not referenced or any(
            t.schema != "swarm" or t.name not in tables for t in referenced
        ):
            raise Inconclusive("status attempted a non-fixture table read")
        if compiled.schema_translate_map != {"swarm": "pg_temp"}:
            raise Inconclusive("fixture schema translation is missing")
        reads += 1
        if reads > MAX_READS or time.monotonic() - started >= MAX_SECONDS:
            raise Inconclusive("read budget exhausted")

    samples = []
    event.listen(connection, "before_cursor_execute", budget)
    try:
        for index in range(WARMUP + SAMPLES):
            session.expunge_all()
            before = reads
            sample_start = time.monotonic()
            with tracer.start_as_current_span(
                "swarm.synthetic.status",
                attributes={**identity, "synthetic.scenario": "historical-status"},
            ) as span:
                correct = _correct(status(session=session))
                sample = {
                    "warmup": index < WARMUP,
                    "correct": correct,
                    "sql_reads": reads - before,
                    "duration_ms": round((time.monotonic() - sample_start) * 1000, 3),
                }
                sample["work_within_baseline"] = sample["sql_reads"] <= BASELINE_READS
                span.set_attributes({f"synthetic.{k}": v for k, v in sample.items()})
                samples.append(sample)
                if not correct or not sample["work_within_baseline"]:
                    break
    finally:
        event.remove(connection, "before_cursor_execute", budget)
    return samples


def cache_snapshot():
    from factory.orchestration import factory_rollout

    result = {"recent_entries": len(factory_rollout._recent)}
    for name, cache in (
        ("ancestry", factory_rollout._ancestry_status),
        ("files", factory_rollout._immutable_get),
    ):
        info = cache.cache_info()
        result.update(
            {
                f"{name}_{key}": getattr(info, key)
                for key in ("hits", "misses", "currsize")
            }
        )
    return result


def verify(engine, *, run_id, chart_version, image, source_commit, promotion):
    """One bounded run. Caller provenance is labelled, never a deployment fact."""
    UUID(run_id)
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValueError(
            "source_commit must be a full source SHA from publication evidence"
        )
    if (
        not chart_version
        or chart_version != os.environ.get("MONOLITH_CHART_VERSION")
        or not image
        or image != os.environ.get("MONOLITH_IMAGE")
    ):
        raise Inconclusive("deployed chart/image differs from the requested evidence")
    if engine.dialect.name != "postgresql":
        raise Inconclusive("production verification requires PostgreSQL")
    memory_before = process_memory_snapshot()
    used, limit = (
        memory_before["cgroup_memory_bytes"],
        memory_before["cgroup_memory_limit_bytes"],
    )
    if used is None or limit is None or used >= limit * 0.8:
        raise Inconclusive("cgroup headroom is unknown or below 20 percent")
    identity = {
        "synthetic.run_id": run_id,
        "synthetic.fixture": FIXTURE_VERSION,
        "synthetic.application": "monolith",
        "synthetic.chart_version": chart_version,
        "synthetic.image": image,
        "synthetic.source_commit": source_commit,
        "synthetic.kargo_promotion": promotion,
        "synthetic.provenance": "operator_supplied_source_commit",
        "synthetic.process_id": os.getpid(),
        "synthetic.process_instance": PROCESS_INSTANCE,
        "synthetic.pod": os.environ.get("HOSTNAME", "unknown"),
    }
    started = time.monotonic()
    cache_before = cache_snapshot()
    # One dedicated connection bounds acquisition and closes temporary state
    # even if a future reader accidentally ends its transaction.
    isolated = create_engine(
        engine.url, connect_args={"connect_timeout": 2}, poolclass=NullPool
    )

    def deadline(*_):
        if time.monotonic() - started >= MAX_SECONDS:
            raise Inconclusive("run deadline exhausted")

    event.listen(isolated, "before_cursor_execute", deadline)
    with tracer.start_as_current_span(
        "swarm.synthetic.run", attributes=identity
    ) as span:
        try:
            with isolated.connect() as connection:
                transaction = connection.begin()
                try:
                    connection.execute(text("SET LOCAL statement_timeout = '1000ms'"))
                    connection.execute(text("SET LOCAL lock_timeout = '100ms'"))
                    connection.execute(
                        text("SET LOCAL idle_in_transaction_session_timeout = '35s'")
                    )
                    if not connection.execute(
                        text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": LOCK_ID}
                    ).scalar():
                        raise Inconclusive("another synthetic run is active")
                    with fixtures(connection) as session:
                        samples = measure(session, identity)
                finally:
                    transaction.rollback()
        finally:
            isolated.dispose()
        cache_samples = cached_reads(identity)
        with tracer.start_as_current_span(
            "swarm.synthetic.cooldown",
            attributes={**identity, "synthetic.scenario": "cooldown-recovery"},
        ) as cooldown_span:
            pacing = cooldown()
            cooldown_span.set_attributes(
                {f"synthetic.{k}": v for k, v in pacing.items()}
            )
        measured = [s for s in samples if not s["warmup"]]
        verdict = (
            "pass"
            if len(measured) == SAMPLES
            and all(s["correct"] and s["work_within_baseline"] for s in samples)
            and len(cache_samples) == 2 * (WARMUP + SAMPLES)
            and all(s["correct"] for s in cache_samples)
            and pacing["correct"]
            else "fail"
        )
        memory_after = process_memory_snapshot()
        cache_after = cache_snapshot()
        span.set_attribute("synthetic.verdict", verdict)
        for phase, snapshot in (("before", memory_before), ("after", memory_after)):
            span.set_attributes(
                {
                    f"synthetic.memory.{phase}.{k}": v
                    for k, v in snapshot.items()
                    if v is not None
                }
            )
        for phase, snapshot in (("before", cache_before), ("after", cache_after)):
            span.set_attributes(
                {f"synthetic.cache.{phase}.{k}": v for k, v in snapshot.items()}
            )
        return {
            "verdict": verdict,
            "identity": identity,
            "samples": samples,
            "cooldown": pacing,
            "cache_samples": cache_samples,
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "memory_before": memory_before,
            "memory_after": memory_after,
            "cache_before": cache_before,
            "cache_after": cache_after,
            "coverage": {
                "status": "production reader, transaction-local PostgreSQL fixtures",
                "cache": "real rollout observation cache with isolated success/failure fixtures",
                "memory": "process/cgroup observations, no retained-growth verdict",
                "recovery": "isolated cooldown only; no process restart",
                "latency": "baseline only; three samples cannot establish a tail percentile",
                "external_api_reads": "none in these read paths",
                "honeycomb_arrival": "unverified",
                "rollback": "manual only",
            },
        }
