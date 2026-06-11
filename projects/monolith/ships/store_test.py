"""Unit tests for ships.store.persist_batch on SQLite.

Uses SQLModel.metadata.create_all (no migrations), mirroring models_test.py.
These exercise the stateless read-back + dedup + batched-upsert path; the dedup
decision itself lives in ships.ais and is tested there. Here we confirm that
persist_batch reads prior state only from the DB, advances prior within a batch,
preserves ship_name, and upserts vessels.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, create_engine, select
from sqlmodel.pool import StaticPool

from ships.models import LatestPosition, Position, Vessel
from ships.store import persist_batch

T0 = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(name="session")
def session_fixture():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
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


def _position(mmsi: str, lat: float, lon: float, **kw) -> dict:
    data = {
        "mmsi": mmsi,
        "lat": lat,
        "lon": lon,
        "speed": kw.get("speed", 0.0),
        "course": kw.get("course"),
        "heading": kw.get("heading"),
        "nav_status": kw.get("nav_status"),
        "ship_name": kw.get("ship_name", ""),
        "recorded_at": kw.get("recorded_at", T0),
    }
    return data


def _count(session, model) -> int:
    return len(session.exec(select(model)).all())


def test_empty_batch_returns_zero_and_writes_nothing(session):
    assert persist_batch(session, [], []) == 0
    assert _count(session, Position) == 0
    assert _count(session, LatestPosition) == 0
    assert _count(session, Vessel) == 0


def test_first_sighting_inserts_position_latest_and_vessel(session):
    pos = _position("111", 51.95, 4.13, speed=10.0, ship_name="Ever Given")
    vessel = {
        "mmsi": "111",
        "imo": "9074729",
        "call_sign": "ABCD",
        "name": "Ever Given",
        "ship_type": 70,
        "dimension_a": 200,
        "dimension_b": 200,
        "dimension_c": 30,
        "dimension_d": 30,
        "destination": "ROTTERDAM",
        "eta": None,
        "draught": 14.5,
    }

    inserted = persist_batch(session, [pos], [vessel])

    assert inserted == 1
    assert _count(session, Position) == 1
    latest = session.get(LatestPosition, "111")
    assert latest is not None
    assert latest.lat == 51.95
    assert latest.ship_name == "Ever Given"
    assert latest.first_seen_at_location.replace(tzinfo=None) == T0.replace(tzinfo=None)
    loaded_vessel = session.get(Vessel, "111")
    assert loaded_vessel is not None
    assert loaded_vessel.name == "Ever Given"
    assert loaded_vessel.imo == "9074729"
    assert loaded_vessel.destination == "ROTTERDAM"


def test_stationary_position_is_skipped(session):
    seeded = LatestPosition(
        mmsi="222",
        lat=10.0,
        lon=20.0,
        speed=0.0,
        ship_name="Anchored",
        recorded_at=T0,
        first_seen_at_location=T0,
    )
    session.add(seeded)
    session.commit()

    # Same spot, speed 0, only 100 s later (under the 300 s time threshold).
    pos = _position(
        "222", 10.0, 20.0, speed=0.0, recorded_at=T0 + timedelta(seconds=100)
    )
    inserted = persist_batch(session, [pos], [])

    assert inserted == 0
    assert _count(session, Position) == 0
    latest = session.get(LatestPosition, "222")
    assert latest.recorded_at.replace(tzinfo=None) == T0.replace(
        tzinfo=None
    )  # unchanged


def test_moving_vessel_position_is_inserted_and_latest_updated(session):
    seeded = LatestPosition(
        mmsi="333",
        lat=10.0,
        lon=20.0,
        speed=0.0,
        ship_name="Mover",
        recorded_at=T0,
        first_seen_at_location=T0,
    )
    session.add(seeded)
    session.commit()

    new_time = T0 + timedelta(seconds=30)
    pos = _position(
        "333", 10.001, 20.001, speed=8.0, ship_name="Mover", recorded_at=new_time
    )
    inserted = persist_batch(session, [pos], [])

    assert inserted == 1
    assert _count(session, Position) == 1
    latest = session.get(LatestPosition, "333")
    assert latest.recorded_at.replace(tzinfo=None) == new_time.replace(tzinfo=None)
    assert latest.speed == 8.0


def test_intra_batch_dedup_against_just_accepted_position(session):
    # First position is a first sighting (accepted); the second is at the same
    # spot, stationary, within the time threshold relative to the FIRST. The
    # working prior must have advanced to the first, so the second dedups.
    first = _position("444", 30.0, 40.0, speed=0.0, recorded_at=T0)
    second = _position(
        "444", 30.0, 40.0, speed=0.0, recorded_at=T0 + timedelta(seconds=60)
    )

    inserted = persist_batch(session, [first, second], [])

    assert inserted == 1
    assert _count(session, Position) == 1
    latest = session.get(LatestPosition, "444")
    assert latest.recorded_at.replace(tzinfo=None) == T0.replace(
        tzinfo=None
    )  # only the first survived


def test_ship_name_preserved_when_new_position_has_none(session):
    seeded = LatestPosition(
        mmsi="555",
        lat=10.0,
        lon=20.0,
        speed=0.0,
        ship_name="Maersk",
        recorded_at=T0,
        first_seen_at_location=T0,
    )
    session.add(seeded)
    session.commit()

    # A clearly-moving update (so it is inserted) but with no ship_name.
    new_time = T0 + timedelta(seconds=30)
    pos = _position(
        "555", 10.001, 20.001, speed=9.0, ship_name="", recorded_at=new_time
    )
    inserted = persist_batch(session, [pos], [])

    assert inserted == 1
    latest = session.get(LatestPosition, "555")
    assert latest.ship_name == "Maersk"  # not nulled out


def test_vessel_coalesce_preserves_existing_on_falsy(session):
    session.add(
        Vessel(
            mmsi="666",
            name="Original",
            call_sign="CALL",
            destination="HAMBURG",
            draught=12.0,
        )
    )
    session.commit()

    # Empty strings / None must not clobber existing values.
    vessel = {
        "mmsi": "666",
        "imo": None,
        "call_sign": "",
        "name": "",
        "ship_type": 80,
        "dimension_a": None,
        "dimension_b": None,
        "dimension_c": None,
        "dimension_d": None,
        "destination": "",
        "eta": None,
        "draught": None,
    }
    persist_batch(session, [], [vessel])

    loaded = session.get(Vessel, "666")
    assert loaded.name == "Original"
    assert loaded.call_sign == "CALL"
    assert loaded.destination == "HAMBURG"
    assert loaded.draught == 12.0
    assert loaded.ship_type == 80  # new structured value applied
