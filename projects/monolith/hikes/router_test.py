"""Unit tests for hikes/router.py: /api/hikes/walks (+ /{uuid} detail).

Uses an in-memory SQLite DB seeded with real rows and a minimal FastAPI app
that mounts only the hikes router, mirroring the schema-stripping +
``app.dependency_overrides[get_session]`` pattern in ``stars/router_test.py``.

Hours are anchored on ``top_of_hour(now)`` so the read-time cutoff keeps the
seeded "future" rows and drops the seeded "elapsed" one, the same way
stars/router_test pins its timestamps to the cutoff.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

from core.db import get_session
from hikes.models import Walk, WalkHour
from hikes.router import _doable_days, _longest_good_run, router
from shared.forecast_freshness import top_of_hour

CUTOFF = top_of_hour(datetime.now(timezone.utc))
H1 = CUTOFF + timedelta(hours=1)
ELAPSED = CUTOFF - timedelta(hours=1)
FETCHED = datetime(2026, 6, 12, 6, 0, 0, tzinfo=timezone.utc)

BEN = "aaaaaaaa-0000-5000-8000-000000000001"
TEALLACH = "aaaaaaaa-0000-5000-8000-000000000002"

# Mid-morning UK anchor for the pure-helper unit tests: 06:00 UTC is 07:00 BST in
# June, so offsets up to ~10 h stay inside one UK calendar day (no midnight split
# to make run lengths non-deterministic).
UNIT_BASE = datetime(2026, 6, 15, 6, 0, 0, tzinfo=timezone.utc)


def _hours_at(base: datetime, *offsets: int) -> list[datetime]:
    """Datetimes ``base + offset hours`` for each offset (good-hour timestamps)."""
    return [base + timedelta(hours=o) for o in offsets]


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # SQLite can't span schemas, so strip the Postgres-only schema= overrides so
    # SQLModel.metadata.create_all() lands every table in the default schema.
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            yield session
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


@pytest.fixture(name="client")
def client_fixture(session):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = lambda: session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _hour(
    walk_uuid, hour_time, temp_c=12.5, precip_mm=0.0, wind_kmh=14.0, cloud_pct=40.0
):
    return WalkHour(
        walk_uuid=walk_uuid,
        hour_time=hour_time,
        temp_c=temp_c,
        precip_mm=precip_mm,
        wind_kmh=wind_kmh,
        cloud_pct=cloud_pct,
        fetched_at=FETCHED,
    )


def _seed_run(session: Session, walk_uuid: str, count: int, start: datetime = H1):
    """Seed ``count`` consecutive good hours from ``start`` (one per clock hour)."""
    session.add_all(
        [_hour(walk_uuid, start + timedelta(hours=k)) for k in range(count)]
    )


def _seed_walks(session: Session) -> None:
    # Ben Vorlich (5.5 h) gets a long run of good hours so it is doable today
    # regardless of where the UK-midnight boundary falls in the seeded run (12
    # consecutive hours leave at least 6 on either side of any single split, and
    # a 5.5 h walk needs only 5). An Teallach (10 h) has no forecast at all.
    session.add(
        Walk(
            uuid=BEN,
            name="Ben Vorlich from Ardlui",
            url="https://www.walkhighlands.co.uk/lochlomond/ben-vorlich-ardlui.shtml",
            distance_km=11.0,
            ascent_m=920,
            duration_h=5.5,
            summary="A steep but rewarding Munro above Loch Lomond.",
            latitude=56.2734,
            longitude=-4.7521,
        )
    )
    session.add(
        Walk(
            uuid=TEALLACH,
            name="An Teallach circuit",
            url="https://www.walkhighlands.co.uk/torridon/an-teallach.shtml",
            distance_km=18.5,
            ascent_m=1450,
            duration_h=10.0,
            summary="",
            latitude=57.8067,
            longitude=-5.2596,
        )
    )
    _seed_run(session, BEN, 12)
    session.commit()


class TestWalks:
    def test_payload_shape_and_ordering(self, client, session):
        _seed_walks(session)
        r = client.get("/api/hikes/walks")
        assert r.status_code == 200
        body = r.json()
        assert body["count"] == 2
        assert body["generated_at"] == FETCHED.isoformat()
        assert len(body["walks"]) == 2

        # Ordered by name: "An Teallach circuit" before "Ben Vorlich from Ardlui".
        names = [w["name"] for w in body["walks"]]
        assert names == ["An Teallach circuit", "Ben Vorlich from Ardlui"]

        ben = body["walks"][1]
        assert ben["uuid"] == BEN
        assert ben["url"].endswith("ben-vorlich-ardlui.shtml")
        assert ben["distance_km"] == 11.0
        assert ben["ascent_m"] == 920
        assert ben["duration_h"] == 5.5
        assert ben["latitude"] == 56.2734
        assert ben["longitude"] == -4.7521
        # The light list carries viable_days (now duration-aware doability), not
        # the hourly windows or summary (those live on the detail endpoint). The
        # 12-hour run makes the 5.5 h walk doable; the exact day(s) depend on
        # where UK midnight falls in the run, so assert non-empty and bounded by
        # the run's UK days rather than pinning a date.
        uk = ZoneInfo("Europe/London")
        run_days = {
            (H1 + timedelta(hours=k)).astimezone(uk).date().isoformat()
            for k in range(12)
        }
        assert ben["viable_days"]
        assert set(ben["viable_days"]) <= run_days
        assert "windows" not in ben
        assert "summary" not in ben

        # The walk without a forecast has no doable days.
        an_teallach = body["walks"][0]
        assert an_teallach["viable_days"] == []

    def test_elapsed_hours_excluded_from_doable_days(self, client, session):
        # Hours below the clock-hour cutoff must not contribute, even though the
        # prune job has not run: a long run wholly in the past (which WOULD be
        # doable if counted) yields no doable day.
        session.add(
            Walk(
                uuid=BEN,
                name="Ben Vorlich from Ardlui",
                url="https://example.invalid/ben.shtml",
                distance_km=11.0,
                ascent_m=920,
                duration_h=5.5,
                summary="",
                latitude=56.2734,
                longitude=-4.7521,
            )
        )
        _seed_run(session, BEN, 12, start=ELAPSED - timedelta(hours=11))
        session.commit()

        body = client.get("/api/hikes/walks").json()
        assert body["walks"][0]["viable_days"] == []

    def test_cache_and_etag_headers(self, client, session):
        _seed_walks(session)
        r = client.get("/api/hikes/walks")
        assert r.status_code == 200
        assert (
            r.headers["Cache-Control"]
            == "public, max-age=0, s-maxage=1800, stale-while-revalidate=3600, stale-if-error=86400"
        )
        # v4 schema token plus the hourly cutoff are folded into the ETag.
        assert r.headers["ETag"].startswith(f'"v4-{CUTOFF.isoformat()}-')

    def test_conditional_get_returns_304(self, client, session):
        _seed_walks(session)
        first = client.get("/api/hikes/walks")
        etag = first.headers["ETag"]
        second = client.get("/api/hikes/walks", headers={"If-None-Match": etag})
        assert second.status_code == 304
        assert second.headers["ETag"] == etag
        assert second.headers["Cache-Control"]

    def test_empty_corpus(self, client):
        r = client.get("/api/hikes/walks")
        assert r.status_code == 200
        assert r.json() == {"count": 0, "generated_at": None, "walks": []}


class TestWalkDetail:
    def test_detail_returns_summary_and_windows(self, client, session):
        _seed_walks(session)
        r = client.get(f"/api/hikes/walks/{BEN}")
        assert r.status_code == 200
        body = r.json()
        assert body["uuid"] == BEN
        assert body["summary"] == "A steep but rewarding Munro above Loch Lomond."
        # Hours reassembled into wire tuples, ordered by time. wind_kmh/cloud_pct
        # come back as ints (legacy tuple shape); temp_c/precip_mm stay numeric.
        # The detail endpoint is NOT duration-gated: it returns every future hour
        # (the seeded 12-hour run), independent of the list's doability test.
        assert body["windows"] == [
            [int((H1 + timedelta(hours=k)).timestamp()), 12.5, 0.0, 14, 40]
            for k in range(12)
        ]
        assert r.headers["Cache-Control"]

    def test_detail_excludes_elapsed_hours(self, client, session):
        session.add(
            Walk(
                uuid=BEN,
                name="Ben Vorlich from Ardlui",
                url="https://example.invalid/ben.shtml",
                distance_km=11.0,
                ascent_m=920,
                duration_h=5.5,
                summary="",
                latitude=56.2734,
                longitude=-4.7521,
            )
        )
        session.add(_hour(BEN, ELAPSED))
        session.add(_hour(BEN, H1))
        session.commit()

        body = client.get(f"/api/hikes/walks/{BEN}").json()
        times = [w[0] for w in body["windows"]]
        assert times == [int(H1.timestamp())]

    def test_detail_404_for_unknown_uuid(self, client, session):
        _seed_walks(session)
        r = client.get("/api/hikes/walks/does-not-exist")
        assert r.status_code == 404


class TestLongestGoodRun:
    def test_empty(self):
        assert _longest_good_run([]) == 0

    def test_single(self):
        assert _longest_good_run([100]) == 1

    def test_contiguous(self):
        assert _longest_good_run([100, 101, 102, 103]) == 4

    def test_one_gap_is_bridged(self):
        # Missing hour 102 (bad weather) is tolerated once: 100,101,_,103,104.
        assert _longest_good_run([100, 101, 103, 104]) == 4

    def test_second_gap_ends_the_run(self):
        # 100,102,104: first gap bridged (run 2), second gap breaks it.
        assert _longest_good_run([100, 102, 104]) == 2

    def test_two_hour_gap_is_not_bridged(self):
        # Two missing hours (102,103) is too long: the run ends at 101.
        assert _longest_good_run([100, 101, 104, 105]) == 2

    def test_best_run_wins_after_break(self):
        # 100,101,_,103,104 (run 4) then break, then 106,107 (run 2): best is 4.
        assert _longest_good_run([100, 101, 103, 104, 107, 108]) == 4


class TestDoableDays:
    def test_run_meets_eighty_percent_floor(self):
        # 5.5 h walk needs 0.8 * 5.5 = 4.4 -> 5 good hours; a 5-hour run qualifies.
        hours = _hours_at(UNIT_BASE, 0, 1, 2, 3, 4)
        assert _doable_days(hours, 5.5) == ["2026-06-15"]

    def test_run_below_floor_is_not_doable(self):
        # Four good hours falls short of the 4.4 floor for a 5.5 h walk.
        hours = _hours_at(UNIT_BASE, 0, 1, 2, 3)
        assert _doable_days(hours, 5.5) == []

    def test_exact_whole_hour_floor_with_float_epsilon(self):
        # 5.0 h walk needs exactly 4.0 good hours; the epsilon keeps 0.8*5.0 from
        # rejecting a 4-hour run on float drift.
        hours = _hours_at(UNIT_BASE, 0, 1, 2, 3)
        assert _doable_days(hours, 5.0) == ["2026-06-15"]

    def test_one_bad_hour_in_slot_still_doable(self):
        # 8,9,10,_,12,13 -> bridged run of 5 good hours clears the 4.4 floor.
        hours = _hours_at(UNIT_BASE, 0, 1, 2, 4, 5)
        assert _doable_days(hours, 5.5) == ["2026-06-15"]

    def test_long_walk_gets_dark_shoulder_credit(self):
        # 10 h walk needs 0.8 * 10 = 8 hours; a 6-hour daylight run + 2 shoulder
        # hours (>7 h walk) reaches 8 and is doable.
        hours = _hours_at(UNIT_BASE, 0, 1, 2, 3, 4, 5)
        assert _doable_days(hours, 10.0) == ["2026-06-15"]

    def test_long_walk_without_enough_run_not_doable(self):
        # Five daylight hours + 2 shoulder = 7 < 8: not doable for a 10 h walk.
        hours = _hours_at(UNIT_BASE, 0, 1, 2, 3, 4)
        assert _doable_days(hours, 10.0) == []

    def test_short_walk_no_shoulder_credit(self):
        # 7.0 h is NOT > LONG_WALK_HOURS, so no shoulder: needs 0.8*7=5.6 -> 6
        # good hours. A 5-hour run does not qualify.
        hours = _hours_at(UNIT_BASE, 0, 1, 2, 3, 4)
        assert _doable_days(hours, 7.0) == []

    def test_no_hours_not_doable(self):
        assert _doable_days([], 3.0) == []
