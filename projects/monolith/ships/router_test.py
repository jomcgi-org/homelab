"""Unit tests for ships/router.py: /api/ships/snapshot and /track/{mmsi}.

Uses a file-backed SQLite DB seeded with real rows and a minimal FastAPI app
that mounts only the ships router.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import fastapi.routing
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlmodel import SQLModel, Session, create_engine

from core.db import get_session
from ships.models import (
    HeatCell,
    HeatCellHistorical,
    LatestPosition,
    Position,
    Vessel,
)
from ships.router import _log_stops, _merge_cells, _parse_since, router

T0 = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'ships.db'}",
        connect_args={"check_same_thread": False},
    ).execution_options(schema_translate_map={"ships": None})
    SQLModel.metadata.create_all(
        engine,
        tables=[
            model.__table__
            for model in (
                Vessel,
                Position,
                LatestPosition,
                HeatCell,
                HeatCellHistorical,
            )
        ],
    )
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture(name="client")
def client_fixture(session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _seed_snapshot(session: Session) -> None:
    session.add(
        Vessel(mmsi="316001234", name="ALPHA", ship_type=70, destination="VANCOUVER")
    )
    session.add(Vessel(mmsi="316005678", name="BRAVO", ship_type=80))
    session.add(
        LatestPosition(
            mmsi="316001234",
            lat=49.28,
            lon=-123.12,
            speed=12.5,
            course=180.0,
            heading=182,
            nav_status=0,
            ship_name="ALPHA POS",
            recorded_at=T0,
            first_seen_at_location=T0 - timedelta(hours=2),
            updated_at=T0,
        )
    )
    session.add(
        LatestPosition(
            mmsi="316005678",
            lat=48.5,
            lon=-122.0,
            speed=0.0,
            recorded_at=T0 + timedelta(minutes=5),
            updated_at=T0 + timedelta(minutes=5),
        )
    )
    session.commit()


class TestSnapshot:
    def test_returns_merged_vessels(self, client, session):
        _seed_snapshot(session)
        r = client.get("/api/ships/snapshot")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 2
        assert len(body["vessels"]) == 2

        by_mmsi = {v["mmsi"]: v for v in body["vessels"]}
        alpha = by_mmsi["316001234"]
        # Position-message ship_name wins over the vessel name.
        assert alpha["ship_name"] == "ALPHA POS"
        assert alpha["name"] == "ALPHA"
        assert alpha["ship_type"] == 70
        assert alpha["destination"] == "VANCOUVER"
        assert alpha["lat"] == 49.28
        assert alpha["speed"] == 12.5
        assert alpha["first_seen_at_location"] is not None

        # No position ship_name falls back to the vessel name.
        bravo = by_mmsi["316005678"]
        assert bravo["ship_name"] == "BRAVO"

    def test_cache_and_etag_headers(self, client, session):
        _seed_snapshot(session)
        r = client.get("/api/ships/snapshot")
        assert r.status_code == 200
        assert (
            r.headers["Cache-Control"]
            == "public, max-age=0, s-maxage=120, stale-while-revalidate=600, stale-if-error=86400"
        )
        assert r.headers["ETag"]

    def test_conditional_get_returns_304(self, client, session):
        _seed_snapshot(session)
        first = client.get("/api/ships/snapshot")
        etag = first.headers["ETag"]
        second = client.get("/api/ships/snapshot", headers={"If-None-Match": etag})
        assert second.status_code == 304
        assert second.headers["ETag"] == etag
        assert second.headers["Cache-Control"]

    def test_empty_snapshot(self, client):
        r = client.get("/api/ships/snapshot")
        assert r.status_code == 200
        assert r.json() == {"count": 0, "vessels": []}

    def test_exact_wire_body_preserves_nulls_zeroes_and_name_fallback(
        self, client, session
    ):
        session.add(
            Vessel(
                mmsi="316001234",
                name="ÉTOILE",
                ship_type=0,
                destination="",
                eta=T0 + timedelta(hours=5),
                imo="unused",
                dimension_a=99,
            )
        )
        session.add(
            LatestPosition(
                mmsi="316001234",
                lat=0.0,
                lon=-123.0,
                speed=0.0,
                course=0.0,
                heading=0,
                nav_status=0,
                ship_name="",
                recorded_at=T0,
                updated_at=T0,
            )
        )
        session.commit()

        r = client.get("/api/ships/snapshot")
        assert (
            r.content
            == (
                '{"count":1,"vessels":[{"mmsi":"316001234","lat":0.0,"lon":-123.0,'
                '"speed":0.0,"course":0.0,"heading":0,"nav_status":0,'
                '"ship_name":"ÉTOILE","recorded_at":"2026-06-10T12:00:00+00:00",'
                '"first_seen_at_location":null,"updated_at":"2026-06-10T12:00:00+00:00",'
                '"name":"ÉTOILE","ship_type":0,"destination":"",'
                '"eta":"2026-06-10T17:00:00+00:00"}]}'
            ).encode()
        )
        assert r.headers["content-type"] == "application/json"
        assert int(r.headers["content-length"]) == len(r.content)
        assert r.headers["etag"] == '"2026-06-10T12:00:00+00:00-1"'

    @pytest.mark.parametrize("ship_name", [None, "", "SOLO"])
    def test_keeps_positions_without_vessel_metadata(self, client, session, ship_name):
        session.add(
            LatestPosition(
                mmsi="999001234",
                lat=49.0,
                lon=-123.0,
                ship_name=ship_name,
                recorded_at=T0,
                updated_at=T0,
            )
        )
        session.commit()
        r = client.get("/api/ships/snapshot")
        assert r.status_code == 200
        assert r.json() == {
            "count": 1,
            "vessels": [
                {
                    "mmsi": "999001234",
                    "lat": 49.0,
                    "lon": -123.0,
                    "speed": None,
                    "course": None,
                    "heading": None,
                    "nav_status": None,
                    "ship_name": ship_name or None,
                    "recorded_at": T0.isoformat(),
                    "first_seen_at_location": None,
                    "updated_at": T0.isoformat(),
                    "name": None,
                    "ship_type": None,
                    "destination": None,
                    "eta": None,
                }
            ],
        }

    def test_snapshot_does_not_hydrate_fleet_models(self, client, session):
        _seed_snapshot(session)
        session.expunge_all()
        loaded = []

        def on_load(instance, _context):
            loaded.append(type(instance).__name__)

        for model in (LatestPosition, Vessel):
            event.listen(model, "load", on_load)
        try:
            assert client.get("/api/ships/snapshot").json()["count"] == 2
        finally:
            for model in (LatestPosition, Vessel):
                event.remove(model, "load", on_load)
        assert loaded == []

    def test_snapshot_avoids_recursive_json_copy(self, client, session, monkeypatch):
        _seed_snapshot(session)

        def unexpected_copy(*_args, **_kwargs):
            raise AssertionError("snapshot primitives must not be recursively copied")

        monkeypatch.setattr(fastapi.routing, "jsonable_encoder", unexpected_copy)
        assert client.get("/api/ships/snapshot").json()["count"] == 2

    def test_snapshot_reads_updates_and_removals_without_changing_cache_policy(
        self, client, session
    ):
        _seed_snapshot(session)
        first = client.get("/api/ships/snapshot")
        pos = session.get(LatestPosition, "316001234")
        pos.lat = 50.0
        pos.updated_at = T0 + timedelta(hours=1)
        session.commit()
        updated = client.get(
            "/api/ships/snapshot", headers={"If-None-Match": first.headers["etag"]}
        )
        assert updated.status_code == 200
        assert updated.headers["etag"] != first.headers["etag"]
        assert {v["mmsi"]: v for v in updated.json()["vessels"]}["316001234"][
            "lat"
        ] == 50.0
        session.delete(session.get(LatestPosition, "316005678"))
        session.commit()
        removed = client.get(
            "/api/ships/snapshot", headers={"If-None-Match": updated.headers["etag"]}
        )
        assert removed.status_code == 200
        assert removed.json()["count"] == 1
        assert removed.headers["etag"] != updated.headers["etag"]
        assert removed.headers["cache-control"] == first.headers["cache-control"]
        unchanged = client.get(
            "/api/ships/snapshot", headers={"If-None-Match": removed.headers["etag"]}
        )
        assert unchanged.status_code == 304
        assert unchanged.content == b""
        assert unchanged.headers["cache-control"] == first.headers["cache-control"]


def _seed_track(session: Session) -> None:
    # Seed relative to now: the /track since= filter is computed from
    # datetime.now(), so fixed past timestamps would fall outside any recent
    # window once the calendar advances. base, base-1h, base-2h lets since=90m
    # select exactly the first two.
    base = datetime.now(timezone.utc)
    for i in range(3):
        session.add(
            Position(
                mmsi="316001234",
                lat=49.0 + i,
                lon=-123.0 - i,
                speed=float(i),
                course=10.0 * i,
                heading=i,
                nav_status=0,
                recorded_at=base - timedelta(hours=i),
            )
        )
    session.commit()


class TestTrack:
    def test_newest_first(self, client, session):
        _seed_track(session)
        r = client.get("/api/ships/track/316001234")
        assert r.status_code == 200
        body = r.json()
        assert body["mmsi"] == "316001234"
        assert body["count"] == 3
        recorded = [p["recorded_at"] for p in body["track"]]
        assert recorded == sorted(recorded, reverse=True)

    def test_since_filters_older(self, client, session):
        _seed_track(session)
        # T0 and T0-1h fall within 90 minutes; T0-2h does not.
        r = client.get("/api/ships/track/316001234?since=90m")
        assert r.status_code == 200
        assert r.json()["count"] == 2

    def test_limit_caps(self, client, session):
        _seed_track(session)
        r = client.get("/api/ships/track/316001234?limit=1")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 1

    def test_unknown_mmsi_returns_empty(self, client):
        r = client.get("/api/ships/track/000000000")
        assert r.status_code == 200
        assert r.json() == {"mmsi": "000000000", "count": 0, "track": []}

    def test_cache_header(self, client, session):
        _seed_track(session)
        r = client.get("/api/ships/track/316001234")
        assert (
            r.headers["Cache-Control"]
            == "public, max-age=0, s-maxage=60, stale-while-revalidate=300, stale-if-error=86400"
        )


class TestParseSince:
    def test_hours(self):
        assert _parse_since("1h") == timedelta(hours=1)

    def test_minutes(self):
        assert _parse_since("30m") == timedelta(minutes=30)

    def test_days(self):
        assert _parse_since("2d") == timedelta(days=2)

    def test_invalid_returns_none(self):
        assert _parse_since("bogus") is None
        assert _parse_since("h") is None
        assert _parse_since("") is None
        assert _parse_since(None) is None


def _seed_heat(session: Session) -> None:
    session.add(HeatCell(lat_bin=9856, lon_bin=-16416, count=22))
    session.add(HeatCell(lat_bin=9520, lon_bin=-16312, count=3))
    session.commit()


class TestHeat:
    def test_exact_json_and_cache_headers(self, client, session):
        session.add_all(
            [
                HeatCell(lat_bin=9856, lon_bin=-16416, count=22),
                HeatCellHistorical(lat_bin=9856, lon_bin=-16416, count=100),
                HeatCellHistorical(lat_bin=9520, lon_bin=-16312, count=7),
            ]
        )
        session.commit()

        r = client.get("/api/ships/heat")
        assert r.status_code == 200
        assert r.content == (
            b'{"step_lat":0.005,"step_lon":0.0075,"count":2,'
            b'"cells":[[9856,-16416,122],[9520,-16312,7]],'
            b'"stops":[1,3,7,18,47,122]}'
        )
        assert r.headers["content-type"] == "application/json"
        assert int(r.headers["content-length"]) == len(r.content)
        assert r.headers["cache-control"] == (
            "public, max-age=0, s-maxage=300, "
            "stale-while-revalidate=3600, stale-if-error=86400"
        )

    def test_reads_only_cell_scalars(self, client, session, monkeypatch):
        _seed_heat(session)
        statements = []
        execute = session.exec

        def record(statement, *args, **kwargs):
            statements.append(statement)
            return execute(statement, *args, **kwargs)

        monkeypatch.setattr(session, "exec", record)
        assert client.get("/api/ships/heat").status_code == 200
        assert len(statements) == 2
        assert [
            [(column.table.name, column.name) for column in statement.selected_columns]
            for statement in statements
        ] == [
            [("heat_cells", name) for name in ("lat_bin", "lon_bin", "count")],
            [
                ("heat_cells_historical", name)
                for name in ("lat_bin", "lon_bin", "count")
            ],
        ]

    def test_renders_json_without_a_second_recursive_copy(
        self, client, session, monkeypatch
    ):
        _seed_heat(session)

        def unexpected_copy(*args, **kwargs):
            raise AssertionError("heat JSON primitives must not be recursively copied")

        monkeypatch.setattr(fastapi.routing, "jsonable_encoder", unexpected_copy)
        assert client.get("/api/ships/heat").json()["count"] == 2

    def test_reads_new_rollup_on_each_request(self, client, session):
        cell = HeatCellHistorical(lat_bin=-100, lon_bin=200, count=7)
        session.add(cell)
        session.commit()
        assert client.get("/api/ships/heat").json()["cells"] == [[-100, 200, 7]]

        cell.count = 9
        session.commit()
        assert client.get("/api/ships/heat").json()["cells"] == [[-100, 200, 9]]

    def test_returns_cells_and_steps(self, client, session):
        _seed_heat(session)
        r = client.get("/api/ships/heat")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 2
        assert body["step_lat"] == 0.005
        assert body["step_lon"] == 0.0075
        # Compact [lat_bin, lon_bin, count] triples; client rebuilds polygons.
        triples = {(c[0], c[1]): c[2] for c in body["cells"]}
        assert triples[(9856, -16416)] == 22
        assert triples[(9520, -16312)] == 3

    def test_empty_when_no_rollup(self, client):
        r = client.get("/api/ships/heat")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 0
        assert body["cells"] == []

    def test_merges_live_and_historical_and_emits_stops(self, client, session):
        # Same cell in both tables -> the all-time view sums the two counts.
        session.add(HeatCell(lat_bin=9856, lon_bin=-16416, count=22))
        session.add(HeatCellHistorical(lat_bin=9856, lon_bin=-16416, count=100))
        session.add(HeatCellHistorical(lat_bin=9520, lon_bin=-16312, count=7))
        session.commit()

        r = client.get("/api/ships/heat")
        assert r.status_code == 200
        body = r.json()
        # Two distinct cells; the shared one is summed, not double-counted.
        assert body["count"] == 2
        triples = {(c[0], c[1]): c[2] for c in body["cells"]}
        assert triples[(9856, -16416)] == 122
        assert triples[(9520, -16312)] == 7
        assert "stops" in body
        assert body["stops"]


def test_merge_cells_sums_overlapping_bins():
    live = [(1, 2, 5), (3, 4, 10)]
    hist = [(1, 2, 100), (5, 6, 7)]
    merged = {(la, lo): c for la, lo, c in _merge_cells(live, hist)}
    assert merged[(1, 2)] == 105
    assert merged[(3, 4)] == 10
    assert merged[(5, 6)] == 7


def test_log_stops_ascending_unique_and_capped():
    stops = _log_stops([1, 1, 1, 2, 5, 8, 13, 21, 34, 55, 89, 144])
    assert stops == sorted(set(stops))
    assert len(stops) <= 6
    assert stops[0] == 1


def test_log_stops_empty():
    assert _log_stops([]) == []


def test_log_stops_spreads_skewed_traffic_and_ignores_outlier():
    # The real shape: a mass of count-1 ocean cells, a busy-lane gradient, and
    # one mega port-approach cell. Log spacing must NOT collapse to 2-3 buckets
    # (the linear-quantile failure), and the hot end must track p99, not the
    # 500000 outlier, so a shared channel reads hot without one cell stretching
    # the scale.
    counts = [1] * 900 + list(range(2, 100)) + [500000]
    stops = _log_stops(counts)
    assert stops[0] == 1
    assert stops == sorted(set(stops))
    assert len(stops) >= 4  # does not collapse like linear quantiles did
    assert stops[-1] < 1000  # anchored on p99, well below the outlier


def test_log_stops_all_ones_single_bucket():
    # Every occupied cell seen exactly once: p99 anchor is 1, so a single bucket.
    assert _log_stops([1, 1, 1, 1]) == [1]
