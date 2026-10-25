"""File-backed DB and atomic fake-object evidence for archive-before-prune."""

import io
import json
import os
import subprocess
import threading
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from sqlalchemy import event
from sqlalchemy.dialects import postgresql
from sqlalchemy.sql.dml import Update
from sqlmodel import Session, create_engine, select, update

from stars import archive
from stars.models import HourArchiveCheckpoint, Site, SiteHour, SiteMonthClimatology

NOW = datetime(2026, 10, 25, 12, 45, tzinfo=UTC)


class Storage:
    """Atomic If-None-Match fake; failed PUT may have persisted its bytes."""

    def __init__(self):
        self.objects = {}
        self.lock = threading.Lock()
        self.before_put = lambda key: None
        self.after_put = lambda key: None
        self.on_get = lambda key, body: body

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch, ContentType):
        assert IfNoneMatch == "*"
        self.before_put(Key)
        with self.lock:
            if Key in self.objects:
                raise FileExistsError("412 PreconditionFailed")
            self.objects[Key] = Body
        self.after_put(Key)

    def get_object(self, *, Bucket, Key):
        return {"Body": io.BytesIO(self.on_get(Key, self.objects[Key]))}


@pytest.fixture
def engine(tmp_path):
    db = create_engine(
        f"sqlite:///{tmp_path / 'archive.db'}",
        connect_args={"check_same_thread": False, "timeout": 10},
        execution_options={"schema_translate_map": {"stars": None}},
    )
    for model in (Site, SiteHour, HourArchiveCheckpoint, SiteMonthClimatology):
        model.__table__.create(db)
    yield db
    db.dispose()


def seed(engine, count=3, *, symbol="clear", site="grid-001"):
    with Session(engine) as session:
        if session.get(Site, site) is None:
            session.add(
                Site(
                    id=site,
                    name="Test site",
                    lat=56.5,
                    lon=-4.2,
                    forecast_fetched_at=NOW,
                )
            )
        session.add_all(
            [
                SiteHour(
                    site_id=site,
                    hour_time=NOW.replace(minute=0) - timedelta(hours=i),
                    cloud_area_fraction=9.0,
                    air_temperature=-2.0,
                    dew_spread=3.5,
                    sun_elevation_deg=-18.0,
                    relative_humidity=70.0,
                    wind_speed=4.5,
                    symbol=symbol,
                    fetched_at=NOW - timedelta(days=1),
                )
                for i in range(1, count + 1)
            ]
        )
        session.commit()


def count(engine):
    with Session(engine) as session:
        return len(session.exec(select(SiteHour)).all())


def run(engine, storage, now=NOW):
    return archive.run_archive(engine, storage, "fake", "hours", now)


def configured_storage(monkeypatch, storage):
    from stars import grid

    storage.meta = SimpleNamespace(
        service_model=SimpleNamespace(
            operation_model=lambda name: SimpleNamespace(
                input_shape=SimpleNamespace(members={"IfNoneMatch": None})
            )
        )
    )
    monkeypatch.setattr(grid, "_s3_client", lambda: storage)
    monkeypatch.setattr(archive, "configured_destination", lambda: ("fake", "hours"))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(archive, "datetime", Clock)


def fail(*args):
    raise OSError("injected failure")


def test_roundtrip_bounds_identity_and_completion(engine):
    seed(engine, 300)
    storage = Storage()
    result = run(engine, storage)
    assert result["deleted"] == 300
    assert not result["backlog"]
    marker = json.loads(
        next(v for k, v in storage.objects.items() if k.endswith("complete.json"))
    )
    assert marker["rows"] == 300
    assert marker["archived_at"] == NOW.isoformat()
    assert marker["cutoff"] == NOW.replace(minute=0).isoformat()
    rows = []
    assert len(marker["chunks"]) == 3
    for index, chunk in enumerate(marker["chunks"]):
        body = storage.objects[chunk["key"]]
        assert archive.digest(body) == chunk["sha256"]
        assert len(body) == chunk["bytes"] <= archive.CHUNK_BYTES + archive.ROW_BYTES
        assert f"/{index:04d}-" in chunk["key"]
        data = json.loads(body)
        assert data["batch_id"] == marker["batch_id"]
        assert len(data["rows"]) <= archive.CHUNK_ROWS
        rows.extend(data["rows"])
    assert [r["hour"]["hour_time"] for r in rows] == sorted(
        r["hour"]["hour_time"] for r in rows
    )
    row = rows[0]
    assert row["site"]["id"] == "grid-001" and row["site"]["lat"] == 56.5
    assert row["hour"]["fetched_at"] == (NOW - timedelta(days=1)).isoformat()
    assert set(row["hour"]) == set(SiteHour.model_fields)
    assert row["hour"]["relative_humidity"] == 70
    assert row["hour"]["wind_speed"] == 4.5
    assert "run_time" not in archive.encoded(marker).decode()
    with Session(engine) as session:
        assert (
            session.get(Site, "grid-001").forecast_fetched_at.replace(tzinfo=UTC) == NOW
        )


@pytest.mark.parametrize(
    "phase", ["before", "after", "get", "mismatch", "marker", "marker_after"]
)
def test_failure_and_restart_stable_at_later_clock(engine, phase):
    seed(engine, 260)
    storage = Storage()
    if phase == "before":
        storage.before_put = fail
    elif phase == "after":
        storage.after_put = fail
        storage.on_get = fail
    elif phase == "get":
        storage.on_get = fail
    elif phase == "mismatch":
        storage.on_get = lambda key, body: body + b"x"
    elif phase == "marker":
        storage.before_put = lambda key: (
            fail() if key.endswith("complete.json") else None
        )
    else:
        storage.on_get = lambda key, body: (
            fail() if key.endswith("complete.json") else body
        )
    with pytest.raises((OSError, KeyError, ValueError)):
        run(engine, storage)
    assert count(engine) == 260
    with Session(engine) as session:
        saved = session.get(HourArchiveCheckpoint, 1).payload
    previous = dict(storage.objects)
    storage.before_put = storage.after_put = lambda key: None
    storage.on_get = lambda key, body: body
    result = run(engine, storage, NOW + timedelta(days=2))
    assert result["deleted"] == 260
    assert all(storage.objects[k] == v for k, v in previous.items())
    assert json.loads(saved)["batch_id"] == result["batch_id"]
    assert all(
        json.loads(v)["archived_at"] == NOW.isoformat()
        for v in storage.objects.values()
    )


@pytest.mark.parametrize("target", ["chunk", "marker"])
def test_conflict_never_overwrites_or_deletes(engine, target):
    seed(engine)
    storage = Storage()

    def conflict(key):
        if (target == "marker") == key.endswith("complete.json"):
            storage.objects[key] = b"conflicting evidence"

    storage.before_put = conflict
    with pytest.raises(ValueError, match="conflict"):
        run(engine, storage)
    assert count(engine) == 3
    assert b"conflicting evidence" in storage.objects.values()


def test_uncertain_success_is_reconciled_by_bytes(engine):
    seed(engine)
    storage = Storage()
    storage.after_put = fail
    assert run(engine, storage)["deleted"] == 3


def test_partial_chunks_remain_and_identical_replay(engine):
    seed(engine, 260)
    storage = Storage()
    storage.before_put = lambda key: fail() if "/0001-" in key else None
    with pytest.raises(KeyError):
        run(engine, storage)
    assert len(storage.objects) == 1 and count(engine) == 260
    first = dict(storage.objects)
    storage.before_put = lambda key: None
    assert run(engine, storage)["deleted"] == 260
    assert all(storage.objects[k] == v for k, v in first.items())


def test_duplicate_worker_interleaving_and_refresh_commit(engine):
    seed(engine, 260)
    storage = Storage()
    entered = False

    def interleave(key):
        nonlocal entered
        if entered:
            return
        entered = True
        # This second worker loads the exact committed envelope while the first
        # is between checkpoint and PUT. A refresh commits new row versions.
        with Session(engine) as session:
            session.execute(
                update(SiteHour)
                .where(SiteHour.symbol == "clear")
                .values(symbol="new", fetched_at=NOW)
            )
            session.commit()
        assert run(engine, storage)["deleted"] == 0

    storage.before_put = interleave
    result = run(engine, storage)
    assert result["deleted"] == 0 and result["backlog"]
    assert count(engine) == 260
    assert len(storage.objects) == 4


def test_late_thread_cannot_clear_new_checkpoint_or_delete_replacements(engine):
    seed(engine)
    storage = Storage()
    paused = threading.Event()
    release = threading.Event()
    results = []
    errors = []

    def before_put(key):
        if threading.current_thread().name == "old-archive" and not paused.is_set():
            paused.set()
            assert release.wait(10)

    def worker():
        try:
            results.append(run(engine, storage))
        except Exception as error:  # noqa: BLE001 (transfer worker errors to the asserting test)
            errors.append(error)

    storage.before_put = before_put
    thread = threading.Thread(target=worker, name="old-archive")
    thread.start()
    try:
        assert paused.wait(10)
        assert run(engine, storage)["deleted"] == 3
        seed(engine, symbol="replacement")
        pending = archive.checkpoint(engine, "fake", "hours", NOW + timedelta(hours=1))
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and not errors
    assert results[0]["deleted"] == 0 and count(engine) == 3
    with Session(engine) as session:
        assert json.loads(session.get(HourArchiveCheckpoint, 1).payload) == pending


def test_concurrent_claimant_loser_never_exports_its_selection(engine):
    seed(engine)
    with Session(engine) as session:
        session.add(HourArchiveCheckpoint(id=1))
        session.commit()
    storage = Storage()
    both_selected = threading.Barrier(2)
    winner_claimed = threading.Event()
    loser_finished = threading.Event()
    candidates = {}
    results = {}
    errors = {}

    def before_claim(conn, cursor, statement, parameters, context, executemany):
        compiled = context.compiled
        if (
            compiled is None
            or not isinstance(compiled.statement, Update)
            or compiled.statement.table.name != "hour_archive_checkpoint"
        ):
            return
        payload = context.compiled_parameters[0].get("payload")
        if payload is None:
            return
        name = threading.current_thread().name
        candidates[name] = json.loads(payload)
        both_selected.wait(10)
        if name == "claim-loser":
            assert winner_claimed.wait(10)

    def before_put(key):
        assert threading.current_thread().name == "claim-winner"
        winner_claimed.set()
        assert loser_finished.wait(10)

    def worker():
        name = threading.current_thread().name
        try:
            results[name] = run(engine, storage)
        except Exception as error:  # noqa: BLE001 (transfer thread failures to assertions)
            errors[name] = error
        finally:
            if name == "claim-loser":
                loser_finished.set()

    storage.before_put = before_put
    event.listen(engine, "before_cursor_execute", before_claim)
    threads = [
        threading.Thread(target=worker, name=name)
        for name in ("claim-winner", "claim-loser")
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(15)
    finally:
        event.remove(engine, "before_cursor_execute", before_claim)
    assert all(not thread.is_alive() for thread in threads)
    assert set(errors) == {"claim-loser"}
    assert isinstance(errors["claim-loser"], RuntimeError)
    assert "claimed concurrently" in str(errors["claim-loser"])
    winner = candidates["claim-winner"]["batch_id"]
    loser = candidates["claim-loser"]["batch_id"]
    assert winner != loser
    assert results["claim-winner"]["deleted"] == 3
    assert storage.objects and all(winner in key for key in storage.objects)
    assert all(loser not in key for key in storage.objects)
    assert count(engine) == 0


def test_earlier_refresh_replaces_rows_at_marker_commit(engine):
    from stars.jobs import _write_sites

    seed(engine)
    storage = Storage()
    started = NOW - timedelta(hours=3)

    def refresh(key):
        if key.endswith("complete.json"):
            with Session(engine) as session:
                _write_sites(
                    session,
                    {
                        "grid-001": [
                            {
                                "time": (NOW - timedelta(hours=2)).isoformat(),
                                "cloud_area_fraction": 80.0,
                                "air_temperature": 5.0,
                                "dew_spread": 3.0,
                                "sun_elevation_deg": -18.0,
                                "symbol": "new",
                            }
                        ]
                    },
                    started,
                )
                session.commit()

    storage.before_put = refresh
    assert run(engine, storage)["deleted"] == 0 and count(engine) == 1


@pytest.mark.parametrize("stop_at,remaining", [(1, 260), (2, 132), (3, 4)])
def test_partial_delete_restart_keeps_old_partition(
    engine, monkeypatch, stop_at, remaining
):
    seed(engine, 260)
    storage = Storage()
    real = archive.deletion_statement
    calls = 0

    def interrupted(rows):
        nonlocal calls
        calls += 1
        if calls == stop_at:
            raise RuntimeError("process died after one committed delete transaction")
        yield from real(rows)

    monkeypatch.setattr(archive, "deletion_statement", interrupted)
    with pytest.raises(RuntimeError):
        run(engine, storage)
    assert count(engine) == remaining
    previous = dict(storage.objects)
    monkeypatch.setattr(archive, "deletion_statement", real)
    assert run(engine, storage, NOW + timedelta(days=1))["deleted"] == remaining
    assert storage.objects == previous


@pytest.mark.parametrize(
    "field,value",
    [
        ("symbol", "new"),
        ("fetched_at", NOW),
        ("wind_speed", 12.0),
        ("cloud_area_fraction", 90.0),
    ],
)
def test_exact_version_guard(engine, field, value):
    seed(engine)
    storage = Storage()

    def refresh(key):
        if key.endswith("complete.json"):
            with Session(engine) as session:
                session.execute(update(SiteHour).values(**{field: value}))
                session.commit()

    storage.before_put = refresh
    assert run(engine, storage)["deleted"] == 0
    assert count(engine) == 3


def test_cutoff_empty_non_utc_and_backlog(engine):
    storage = Storage()
    assert run(engine, storage)["rows"] == 0 and not storage.objects
    seed(engine, 1030)
    with Session(engine) as session:
        session.add(
            SiteHour(
                site_id="boundary",
                hour_time=NOW.replace(minute=0),
                cloud_area_fraction=1,
                air_temperature=2,
                dew_spread=3,
            )
        )
        session.commit()
    local_now = NOW.astimezone(timezone(timedelta(hours=5, minutes=30)))
    result = run(engine, storage, local_now)
    assert result["rows"] == archive.MAX_ROWS and result["backlog"]
    markers = [
        json.loads(body)
        for key, body in storage.objects.items()
        if key.endswith("complete.json")
    ]
    assert markers[0]["cutoff"] == NOW.replace(minute=0).isoformat()
    assert run(engine, storage, local_now)["deleted"] == 6
    assert run(engine, storage, local_now)["rows"] == 0
    assert count(engine) == 1


def test_configured_invocation_drains_multiple_bounded_batches(engine, monkeypatch):
    seed(engine, 2 * archive.MAX_ROWS + 6)
    storage = Storage()
    configured_storage(monkeypatch, storage)
    assert archive.run_configured_archive(engine) == 2 * archive.MAX_ROWS + 6
    markers = [
        json.loads(body)
        for key, body in storage.objects.items()
        if key.endswith("complete.json")
    ]
    assert [marker["rows"] for marker in markers] == [1024, 1024, 6]
    assert {marker["cutoff"] for marker in markers} == {
        NOW.replace(minute=0).isoformat()
    }
    assert count(engine) == 0


def test_drain_replays_old_cutoff_then_selects_current_elapsed_rows(
    engine, monkeypatch
):
    seed(engine, 3)
    old = archive.checkpoint(engine, "fake", "hours", NOW - timedelta(hours=2))
    assert sum(map(len, old["chunks"])) == 1
    storage = Storage()
    configured_storage(monkeypatch, storage)
    assert archive.run_configured_archive(engine) == 3
    markers = [
        json.loads(body)
        for key, body in storage.objects.items()
        if key.endswith("complete.json")
    ]
    assert markers[0]["batch_id"] == old["batch_id"]
    assert [marker["rows"] for marker in markers] == [1, 2]
    assert count(engine) == 0


def test_drain_budget_exhaustion_is_failure_with_remaining_rows(engine, monkeypatch):
    seed(engine, archive.MAX_ROWS + 6)
    storage = Storage()
    configured_storage(monkeypatch, storage)
    clock = iter([0, 0, 240])
    monkeypatch.setattr(archive, "monotonic", lambda: next(clock))
    assert archive.DRAIN_SECONDS == 240
    with pytest.raises(RuntimeError, match="drain budget exhausted"):
        archive.run_configured_archive(engine)
    assert count(engine) == 6
    assert sum(key.endswith("complete.json") for key in storage.objects) == 1
    monkeypatch.setattr(archive, "monotonic", lambda: 0)
    assert archive.run_configured_archive(engine) == 6
    assert count(engine) == 0


def test_drain_capacity_exceeds_grid_daily_inflow(monkeypatch):
    storage = Storage()
    configured_storage(monkeypatch, storage)
    # Conservative synthetic 100 rows/s, below the measured single-batch rate.
    # 14k sites * 15 eligible hours/day = 210k rows/day, 8750 per hourly run.
    elapsed = 0.0
    calls = 0
    rows_per_second = 100

    def batch(engine, client, bucket, prefix, now):
        nonlocal elapsed, calls
        elapsed += archive.MAX_ROWS / rows_per_second
        calls += 1
        return {"deleted": archive.MAX_ROWS, "rows": archive.MAX_ROWS, "backlog": True}

    monkeypatch.setattr(archive, "monotonic", lambda: elapsed)
    monkeypatch.setattr(archive, "run_archive", batch)
    with pytest.raises(RuntimeError, match="drain budget exhausted"):
        archive.run_configured_archive(None)
    assert calls > 1
    assert 240 <= elapsed < 300
    assert calls * archive.MAX_ROWS * 24 > 14000 * 15


def test_chunk_byte_count_matches_encoding_without_growing_array_reencoding(
    engine, monkeypatch
):
    seed(engine, 8, symbol="\u2601" * 20)
    monkeypatch.setattr(archive, "CHUNK_BYTES", 1200)
    real_encoded = archive.encoded

    def no_array_reencoding(value):
        assert not isinstance(value, list)
        return real_encoded(value)

    monkeypatch.setattr(archive, "encoded", no_array_reencoding)
    batch = archive.checkpoint(engine, "fake", "hours", NOW)
    assert len(batch["chunks"]) > 1
    assert sum(map(len, batch["chunks"])) == 8
    for chunk in batch["chunks"]:
        assert len(real_encoded(chunk)) <= archive.CHUNK_BYTES


def test_byte_bounds_and_oversized_row(engine):
    seed(engine, 1000, symbol="x" * 1000)
    storage = Storage()
    result = run(engine, storage)
    assert 0 < result["rows"] < 1000 and result["backlog"]
    assert len(storage.objects) <= archive.MAX_CHUNKS + 1
    with Session(engine) as session:
        session.execute(update(SiteHour).values(symbol="x" * 100000))
        session.commit()
    prior = dict(storage.objects)
    with pytest.raises(ValueError, match="text exceeds"):
        run(engine, storage)
    assert storage.objects == prior


def test_sql_predicate_postgres_and_checkpoint_schema():
    row = {"hour": {name: 1 for name in archive.FIELDS}}
    row["hour"].update(
        site_id="site",
        symbol="clear",
        hour_time=NOW.isoformat(),
        fetched_at=NOW.isoformat(),
    )
    compiled = next(archive.deletion_statement([row])).compile(
        dialect=postgresql.dialect()
    )
    assert len(compiled.params) == len(archive.FIELDS)
    for field in archive.FIELDS:
        assert f"site_hours.{field} =" in str(compiled)
    assert HourArchiveCheckpoint.__table__.c.payload.nullable


def render(*overrides):
    chart = Path(__file__).resolve().parents[1] / "chart"
    result = subprocess.run(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            "archive-test",
            str(chart),
            "--set",
            "jobs.image.repository=example.invalid/jobs",
            "--set",
            "jobs.image.tag=test",
            *overrides,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return result


@pytest.mark.parametrize("enabled", [False, True])
def test_real_chart_cli_handler_path(engine, monkeypatch, enabled):
    from app import jobs_main
    from core import db

    from stars import grid, jobs

    seed(engine)
    args = []
    if enabled:
        args = [
            "--set",
            "stars.archiveBeforePrune.enabled=true",
            "--set",
            "stars.archiveBeforePrune.bucket=fake",
            "--set",
            "stars.archiveBeforePrune.prefix=hours",
        ]
    rendered = render(*args)
    assert rendered.returncode == 0, rendered.stderr
    doc = next(
        d
        for d in yaml.safe_load_all(rendered.stdout)
        if d
        and d.get("kind") == "CronWorkflow"
        and d["metadata"]["name"] == "stars-prune-hours"
    )
    spec = doc["spec"]
    assert spec["schedules"] == ["50 * * * *"] and spec["concurrencyPolicy"] == "Forbid"
    assert spec["workflowSpec"]["activeDeadlineSeconds"] == 300
    container = spec["workflowSpec"]["templates"][0]["container"]
    assert container["args"] == ["stars-prune-hours"]
    assert container["resources"]["limits"]["memory"] == "256Mi"
    for key in (
        "SEAWEEDFS_S3_ENDPOINT",
        "S3_ACCESS_KEY_ID",
        "S3_SECRET_ACCESS_KEY",
        "STARS_ARCHIVE_BEFORE_PRUNE",
        "STARS_ARCHIVE_BUCKET",
        "STARS_ARCHIVE_PREFIX",
        "AWS_REGION",
    ):
        monkeypatch.delenv(key, raising=False)
    for entry in container["env"]:
        monkeypatch.setenv(entry["name"], entry.get("value", "fake-secret"))
    storage = Storage()
    if enabled:
        # Real client metadata proves the pinned SDK accepts the conditional
        # input. The object operations themselves always use the local fake.
        import boto3

        storage.meta = boto3.client(
            "s3",
            region_name="auto",
            aws_access_key_id="fake",
            aws_secret_access_key="fake",
        ).meta
        monkeypatch.setattr(grid, "_s3_client", lambda: storage)
    else:
        monkeypatch.setattr(grid, "_s3_client", fail)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    monkeypatch.setattr(jobs, "top_of_hour", lambda: NOW.replace(minute=0))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(archive, "datetime", Clock)
    jobs_main.stars_prune_hours()
    assert count(engine) == 0
    assert bool(storage.objects) == enabled
    with Session(engine) as session:
        assert (session.get(HourArchiveCheckpoint, 1) is not None) == enabled


@pytest.mark.parametrize(
    "overrides",
    [
        ["--set", "sharedS3.enabled=false"],
        ["--set", "stars.archiveBeforePrune.bucket="],
        ["--set", "stars.archiveBeforePrune.prefix="],
    ],
)
def test_enabled_chart_rejects_missing_storage(overrides):
    result = render(
        "--set",
        "stars.archiveBeforePrune.enabled=true",
        "--set",
        "stars.archiveBeforePrune.bucket=fake",
        "--set",
        "stars.archiveBeforePrune.prefix=hours",
        *overrides,
    )
    assert result.returncode != 0 and "stars archive requires" in result.stderr


def test_enabled_handler_missing_configuration_preserves_rows(engine, monkeypatch):
    from core import db

    from stars import jobs

    seed(engine)
    monkeypatch.setattr(db, "get_engine", lambda: engine)
    monkeypatch.setenv("STARS_ARCHIVE_BEFORE_PRUNE", "true")
    monkeypatch.delenv("S3_ACCESS_KEY_ID", raising=False)
    with pytest.raises(ValueError, match="requires storage"):
        jobs._run_prune()
    assert count(engine) == 3


@pytest.mark.parametrize("enabled", [False, True])
def test_public_materialized_bytes_cache_freshness_coverage_unchanged(
    engine, monkeypatch, enabled
):
    from stars import jobs, materialize, router

    seed(engine)
    with Session(engine) as session:
        session.add_all(
            [
                SiteHour(
                    site_id="grid-001",
                    hour_time=NOW.replace(minute=0) + timedelta(hours=i),
                    cloud_area_fraction=3,
                    air_temperature=2,
                    dew_spread=3,
                    sun_elevation_deg=-18,
                    fetched_at=NOW,
                )
                for i in range(3)
            ]
        )
        session.add(
            SiteMonthClimatology(
                site_id="grid-001", month=10, dark_hours=30, clear_dark_hours=10
            )
        )
        session.commit()

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    class PublicStorage:
        def __init__(self):
            self.writes = []

        def put_object(self, **kwargs):
            self.writes.append(kwargs)

    public = PublicStorage()
    monkeypatch.setattr(router, "datetime", Clock)
    monkeypatch.setattr(materialize, "get_engine", lambda: engine)
    monkeypatch.setattr(materialize, "_s3_client", lambda: public)
    materialize._materialize_sync()
    before = public.writes[:]
    public.writes.clear()
    if enabled:
        assert run(engine, Storage())["deleted"] == 3
    else:
        monkeypatch.setattr(jobs, "top_of_hour", lambda: NOW.replace(minute=0))
        with Session(engine) as session:
            assert jobs._prune_elapsed(session) == 3
            session.commit()
    materialize._materialize_sync()
    assert public.writes == before
    assert len(before) == 3
    sites = json.loads(before[0]["Body"])
    assert sites["sites"][0]["forecast_fetched_at"] == NOW.isoformat()
    assert sites["sites"][0]["clear_dark_hours"] == 3
    assert count(engine) == 3


@pytest.mark.parametrize("shared_storage", ["true", "false"])
def test_default_off_shared_storage_gate_and_invalid_boolean(shared_storage):
    result = render("--set", f"sharedS3.enabled={shared_storage}")
    assert result.returncode == 0
    doc = next(
        d
        for d in yaml.safe_load_all(result.stdout)
        if d and d.get("metadata", {}).get("name") == "stars-prune-hours"
    )
    names = {
        item["name"]
        for item in doc["spec"]["workflowSpec"]["templates"][0]["container"]["env"]
    }
    assert not names.intersection(
        {
            "S3_ACCESS_KEY_ID",
            "S3_SECRET_ACCESS_KEY",
            "SEAWEEDFS_S3_ENDPOINT",
            "AWS_REGION",
        }
    )
    bad = render("--set-string", "stars.archiveBeforePrune.enabled=false")
    assert bad.returncode != 0 and "must be a boolean" in bad.stderr
