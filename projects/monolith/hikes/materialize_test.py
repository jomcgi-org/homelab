"""Unit tests for hikes.materialize: the connector's walks.json snapshot."""

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine

import hikes.materialize as materialize
from hikes.models import Walk, WalkHour

NOW = datetime(2026, 10, 20, 12, 30, tzinfo=timezone.utc)
TOP = NOW.replace(minute=0)


def _walk(uuid, **over):
    fields = dict(
        uuid=uuid,
        name=f"Walk {uuid}",
        url=f"https://www.walkhighlands.co.uk/area/{uuid}.shtml",
        distance_km=10.0,
        ascent_m=500,
        duration_h=4.0,
        summary="Route prose that must never leave the monolith.",
        latitude=57.0,
        longitude=-4.0,
    )
    fields.update(over)
    return Walk(**fields)


def _row(uuid, at, precip=0.1, wind=20.0):
    return (uuid, at, 8.0, precip, wind, 40.0, None, 25.0)


def _hour(uuid, at, fetched=NOW):
    return WalkHour(
        walk_uuid=uuid,
        hour_time=at,
        temp_c=8.0,
        precip_mm=0.1,
        wind_kmh=20.0,
        cloud_pct=40.0,
        fetched_at=fetched,
    )


def test_payload_keeps_future_hours_in_order_and_no_prose():
    walks = [_walk("b"), _walk("a"), _walk("zero", duration_h=0.0)]
    hours = [
        _row("a", TOP + timedelta(hours=2)),
        _row("a", TOP),
        _row("a", TOP - timedelta(hours=1)),  # elapsed
        _row("zero", TOP),
    ]
    payload = materialize.build_payload(walks, hours, NOW, NOW)

    assert payload["schema_version"] == materialize.SCHEMA_VERSION
    assert payload["fetched_at"] == NOW.isoformat()
    # The 0 h walk (a real scrape value) is not publishable.
    assert [w["id"] for w in payload["walks"]] == ["a", "b"]
    a, b = payload["walks"]
    assert [h[0] for h in a["hours"]] == [
        int(TOP.timestamp()),
        int((TOP + timedelta(hours=2)).timestamp()),
    ]
    assert a["hours"][0][1:] == [8.0, 0.1, 20.0, 40.0, None, 25.0]
    assert b["hours"] == []
    assert a["url"].startswith("https://www.walkhighlands.co.uk/")
    assert "summary" not in json.dumps(payload)
    assert "Route prose" not in json.dumps(payload)


def test_payload_without_hours_has_null_fetched_at():
    payload = materialize.build_payload([_walk("a")], [], None, NOW)
    assert payload["fetched_at"] is None
    assert payload["walks"][0]["hours"] == []


def test_camping_membership_survives_actual_snapshot_serialization():
    payload = materialize.build_payload(
        [
            _walk("shore", latitude=56.084, longitude=-4.548),
            _walk("park", latitude=57.17, longitude=-3.67),
            _walk("outside", latitude=55.95, longitude=-3.2),
        ],
        [],
        None,
        NOW,
    )
    rows = {w["id"]: w for w in json.loads(json.dumps(payload))["walks"]}
    assert rows["shore"]["camping_membership"]["zones"] == ["East Loch Lomond"]
    assert rows["park"]["camping_membership"] == {"zones": [], "parks": ["Cairngorms"]}
    assert rows["outside"]["camping_membership"] == {"zones": [], "parks": []}
    fixture = Path(
        os.environ.get(
            "CAMPING_WALKS_FIXTURE",
            Path(__file__).resolve().parents[2]
            / "scotland-connector/internal/store/testdata/camping-walks.json",
        )
    )
    assert json.loads(fixture.read_bytes()) == payload


def test_entity_timestamp_serializes_identically_for_sqlite_and_postgres():
    # SQLite returns naive UTC; Postgres can return an aware session offset.
    for at in (NOW.replace(tzinfo=None), NOW.astimezone(timezone(timedelta(hours=2)))):
        payload = materialize.build_payload(
            [_walk("a", forecast_fetched_at=at)], [], NOW, NOW
        )
        assert payload["walks"][0]["forecast_fetched_at"] == NOW.isoformat()


def test_skips_without_an_https_endpoint(monkeypatch):
    monkeypatch.delenv("SEAWEEDFS_S3_ENDPOINT", raising=False)
    monkeypatch.setattr(materialize, "_s3_client", pytest.fail)
    assert materialize.materialize_sync(NOW) is None
    monkeypatch.setenv("SEAWEEDFS_S3_ENDPOINT", "seaweedfs:8333")
    assert materialize.materialize_sync(NOW) is None


@pytest.fixture(name="engine")
def engine_fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/hikes.db")
    schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        yield engine
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in schemas:
                table.schema = schemas[table.name]


def test_writes_the_snapshot_to_the_configured_object(monkeypatch, engine):
    with Session(engine) as session:
        session.add_all(
            [
                _walk("a", forecast_fetched_at=TOP),
                _hour("a", TOP + timedelta(hours=1), fetched=TOP),
            ]
        )
        session.commit()

    puts = []

    class FakeS3:
        def put_object(self, **kwargs):
            puts.append(kwargs)

    monkeypatch.setenv("SEAWEEDFS_S3_ENDPOINT", "https://r2.example.invalid")
    monkeypatch.setenv("STARS_GRID_S3_BUCKET", "stars")
    monkeypatch.setenv("HIKES_WALKS_S3_KEY", "walks.json")
    monkeypatch.setattr("core.db.get_engine", lambda: engine)
    monkeypatch.setattr(materialize, "_s3_client", lambda: FakeS3())

    written = materialize.materialize_sync(NOW)

    assert len(puts) == 1
    put = puts[0]
    assert (put["Bucket"], put["Key"], put["ContentType"]) == (
        "stars",
        "walks.json",
        "application/json",
    )
    assert written == len(put["Body"])
    body = json.loads(put["Body"])
    assert [w["id"] for w in body["walks"]] == ["a"]
    assert len(body["walks"][0]["hours"]) == 1
    # A row stored without the complete forecast's columns emits nulls.
    assert body["walks"][0]["hours"][0][5:] == [None] * 2
    assert body["fetched_at"].startswith("2026-10-20T12:00:00")


def test_serialized_mixed_entity_times_without_any_hour_rows(engine):
    old = NOW - timedelta(days=1)
    with Session(engine) as session:
        session.add_all(
            [
                _walk("fresh", forecast_fetched_at=NOW),
                _walk("stale", forecast_fetched_at=old),
                _walk("never"),
            ]
        )
        session.commit()
        payload = json.loads(json.dumps(materialize._load(session, NOW)))
    assert payload["fetched_at"] == NOW.isoformat()
    by_id = {w["id"]: w for w in payload["walks"]}
    assert by_id["fresh"]["forecast_fetched_at"] == NOW.isoformat()
    assert by_id["stale"]["forecast_fetched_at"] == old.isoformat()
    assert by_id["never"]["forecast_fetched_at"] is None
    assert all(w["hours"] == [] for w in by_id.values())


def test_payload_keeps_bad_daylight_hours():
    """Schema v2 carries every stored daylight hour, good or not: the
    connector applies the viability rule, or the caller's limits, itself."""
    walks = [_walk("a")]
    hours = [
        _row("a", TOP + timedelta(hours=1)),
        _row("a", TOP + timedelta(hours=2), precip=3.4),
        _row("a", TOP + timedelta(hours=3), wind=91.08),
    ]
    payload = materialize.build_payload(walks, hours, NOW, NOW)
    assert payload["schema_version"] == 2
    got = payload["walks"][0]["hours"]
    assert [len(h) for h in got] == [7, 7, 7]
    assert [h[2] for h in got] == [0.1, 3.4, 0.1]
    assert got[2][3] == 91.08
