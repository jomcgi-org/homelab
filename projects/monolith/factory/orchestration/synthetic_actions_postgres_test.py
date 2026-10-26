"""Real PostgreSQL isolation, work budgets and deliberately broken readers."""

from contextlib import contextmanager
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlmodel import create_engine

from factory.orchestration import synthetic_actions as actions


@pytest.fixture
def engine(pg):
    engine = create_engine(pg.url)
    yield engine
    engine.dispose()


@pytest.fixture
def run_request(monkeypatch):
    monkeypatch.setenv("MONOLITH_CHART_VERSION", "1.2.3")
    monkeypatch.setenv("MONOLITH_IMAGE", "example@sha256:" + "b" * 64)
    monkeypatch.setattr(
        actions,
        "process_memory_snapshot",
        lambda: {
            "rss_bytes": 100,
            "peak_rss_bytes": 100,
            "cgroup_memory_bytes": 100,
            "cgroup_memory_limit_bytes": 1000,
            "cgroup_oom_kills": 0,
        },
    )
    return dict(
        run_id=str(uuid4()),
        chart_version="1.2.3",
        image="example@sha256:" + "b" * 64,
        source_commit="a" * 40,
        promotion="prod.synthetic",
    )


def test_real_status_work_baseline_and_temp_cleanup(engine, run_request, monkeypatch):
    import httpx

    def no_external(*args, **kwargs):
        raise AssertionError("synthetics attempted external HTTP")

    monkeypatch.setattr(httpx.Client, "send", no_external)
    monkeypatch.setattr(httpx.AsyncClient, "send", no_external)
    original = actions.fixtures

    @contextmanager
    def inspect(connection):
        with original(connection) as session:
            tables = (
                connection.execute(
                    text(
                        "SELECT relpersistence FROM pg_class WHERE relnamespace = pg_my_temp_schema() AND relkind = 'r'"
                    )
                )
                .scalars()
                .all()
            )
            assert len(tables) == len(actions._models())
            assert set(tables) == {"t"}
            # Another connection cannot see the fixture, including mid-run.
            with engine.connect() as other:
                assert (
                    other.execute(
                        text("SELECT to_regclass('pg_temp.factory_receipt')")
                    ).scalar()
                    is None
                )
            yield session
        # verify owns the outer transaction; fixture creation is uncommitted.
        assert connection.in_transaction()

    monkeypatch.setattr(actions, "fixtures", inspect)
    for _ in range(2):
        result = actions.verify(engine, **run_request)
        assert result["verdict"] == "pass"
        assert len(result["samples"]) == actions.WARMUP + actions.SAMPLES
        assert result["samples"][0]["warmup"]
        assert all(s["sql_reads"] <= actions.BASELINE_READS for s in result["samples"])
        assert result["cooldown"] == {
            "correct": True,
            "deferred": 3,
            "external_api_reads": 0,
        }
    assert all(m.__table__.schema == "swarm" for m in actions._models())


def test_dropped_history_is_a_failure(engine, run_request, monkeypatch):
    from factory.orchestration import factory_controls

    original = factory_controls.status

    def dropped(**kwargs):
        result = original(**kwargs)
        result["receipts"].pop()
        return result

    monkeypatch.setattr(factory_controls, "status", dropped)
    result = actions.verify(engine, **run_request)
    assert result["verdict"] == "fail"
    assert len(result["samples"]) == 1


def test_extra_sql_work_fails_without_a_latency_threshold(
    engine, run_request, monkeypatch
):
    from factory.orchestration import factory_controls

    original = factory_controls.status

    def repeated(**kwargs):
        # A status regression can return identical data while doing more work.
        result = original(**kwargs)
        original(**kwargs)
        return result

    monkeypatch.setattr(factory_controls, "status", repeated)
    result = actions.verify(engine, **run_request)
    assert result["verdict"] == "fail"
    assert not result["samples"][0]["work_within_baseline"]


@pytest.mark.parametrize(
    "statement",
    ["DELETE FROM pg_temp.factory_receipt", "SELECT * FROM swarm.factory_receipt"],
)
def test_reader_cannot_write_or_read_persistent_schema(
    engine, run_request, monkeypatch, statement
):
    from factory.orchestration import factory_controls

    monkeypatch.setattr(
        factory_controls, "status", lambda **kw: kw["session"].execute(text(statement))
    )
    with pytest.raises(actions.Inconclusive):
        actions.verify(engine, **run_request)
    # Failure released the cross-replica lock.
    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": actions.LOCK_ID}
        ).scalar()


def test_concurrent_run_is_refused(engine, run_request):
    with engine.connect() as connection:
        connection.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": actions.LOCK_ID}
        )
        with pytest.raises(actions.Inconclusive, match="another synthetic"):
            actions.verify(engine, **run_request)


@pytest.mark.parametrize(
    "field,value", [("chart_version", "0.0.1"), ("image", "different")]
)
def test_stale_identity_never_opens_a_connection(
    engine, run_request, field, value, monkeypatch
):
    run_request[field] = value
    monkeypatch.setattr(
        actions, "create_engine", lambda *a, **kw: pytest.fail("connected")
    )
    with pytest.raises(actions.Inconclusive, match="chart/image"):
        actions.verify(engine, **run_request)


@pytest.mark.parametrize("used,limit", [(None, 1000), (100, None), (800, 1000)])
def test_missing_or_low_memory_headroom_never_runs(
    engine, run_request, monkeypatch, used, limit
):
    monkeypatch.setattr(
        actions,
        "process_memory_snapshot",
        lambda: {"cgroup_memory_bytes": used, "cgroup_memory_limit_bytes": limit},
    )
    monkeypatch.setattr(
        actions, "create_engine", lambda *a, **kw: pytest.fail("connected")
    )
    with pytest.raises(actions.Inconclusive, match="headroom"):
        actions.verify(engine, **run_request)


def test_deadline_stops_work_and_releases_lock(engine, run_request, monkeypatch):
    monkeypatch.setattr(actions, "MAX_SECONDS", 0)
    with pytest.raises(actions.Inconclusive, match="deadline"):
        actions.verify(engine, **run_request)


def test_cooldown_regression_is_visible(monkeypatch):
    monkeypatch.setattr(actions.GitHubReadCooldown, "check", lambda self: None)
    assert actions.cooldown()["correct"] is False


def test_traces_bind_samples_to_identity_and_exclude_warmup(
    engine, run_request, monkeypatch
):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(actions, "tracer", provider.get_tracer("synthetics-test"))
    result = actions.verify(engine, **run_request)
    spans = exporter.get_finished_spans()
    assert len(spans) == actions.WARMUP + actions.SAMPLES + 2
    for span in spans:
        assert span.attributes["synthetic.run_id"] == run_request["run_id"]
        assert span.attributes["synthetic.image"] == run_request["image"]
        assert (
            span.attributes["synthetic.source_commit"] == run_request["source_commit"]
        )
        assert span.attributes["synthetic.kargo_promotion"] == run_request["promotion"]
    samples = [s for s in spans if s.name == "swarm.synthetic.status"]
    assert sum(not s.attributes["synthetic.warmup"] for s in samples) == actions.SAMPLES
    root = next(s for s in spans if s.name == "swarm.synthetic.run")
    assert root.attributes["synthetic.verdict"] == result["verdict"]
    assert root.attributes["synthetic.memory.after.rss_bytes"] == 100
    provider.shutdown()
