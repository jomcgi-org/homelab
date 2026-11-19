"""Replay shared AIS input through the hub code and the factory writer.

The hub database gets the monolith migrations from shared.testing.plugin. Each
factory database starts from the chart's bootstrap SQL and is owned by the
ships role, as CloudNativePG creates it. The hub's serving code reads both
databases, so the snapshot, track and heat products compare directly.
"""

import asyncio
import json
import os
import signal
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Queue

import pytest
from fastapi import Request, Response
from sqlmodel import Session, create_engine, text
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

from ships.ais import parse_message
from ships.heat import heat_rollup_handler
from ships.retention import create_partition_sql, partition_maintenance_handler
from ships.router import get_heat, get_snapshot, get_track
from ships.store import persist_batch

UTC = timezone.utc
# The test PostgreSQL has no zone files. It resolves GMT without them.
UTC_SESSION = {"options": "-c timezone=GMT"}
LOCK_WAIT = (
    "select count(*) from pg_stat_activity"
    " where wait_event_type = 'Lock' and query like 'with added%'"
)


class Feed:
    """A local AISStream stand-in that hands each subscribed socket to the test."""

    def __init__(self):
        self.sockets = Queue()
        self.server = serve(self._connected, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"ws://127.0.0.1:{self.server.socket.getsockname()[1]}"

    def _connected(self, ws):
        ws.recv(timeout=10)  # The writer subscribes before the feed sends.
        self.sockets.put(ws)
        try:
            for _ in ws:
                pass
        except ConnectionClosed:
            pass

    def accept(self, timeout=45):
        return self.sockets.get(timeout=timeout)


@pytest.fixture
def feed():
    stand_in = Feed()
    yield stand_in
    stand_in.server.shutdown()


@pytest.fixture
def writers():
    started = []

    def start(dsn, url, retention=False):
        env = dict(
            os.environ,
            DATABASE_URL=dsn,
            AISSTREAM_API_KEY="fixture",
            AISSTREAM_URL=url,
            SHIPS_RETENTION_ENABLED="true" if retention else "false",
        )
        process = subprocess.Popen(
            [os.path.abspath(os.environ["SHIPS_BINARY"])], env=env
        )
        started.append(process)
        return process

    yield start
    for process in started:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)


def factory_database(pg, name):
    """Create a database the way the chart's CloudNativePG bootstrap does."""
    base = pg.url.rsplit("/", 1)[0]
    admin = create_engine(f"{base}/postgres")
    with admin.connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(
            text(
                "DO $$ BEGIN CREATE ROLE ships LOGIN;"
                " EXCEPTION WHEN duplicate_object THEN NULL; END $$"
            )
        )
        conn.execute(text(f"CREATE DATABASE {name} OWNER ships"))
    admin.dispose()
    superuser = create_engine(f"{base}/{name}")
    with superuser.begin() as conn:
        conn.exec_driver_sql(Path(os.environ["SHIPS_BOOTSTRAP_SQL"]).read_text())
    superuser.dispose()
    engine = create_engine(
        f"postgresql+psycopg://ships@127.0.0.1:{pg.port}/{name}",
        connect_args=UTC_SESSION,
    )
    return engine, f"postgres://ships@127.0.0.1:{pg.port}/{name}?sslmode=disable"


def scalar(engine, query):
    with engine.connect() as conn:
        return conn.execute(text(query)).scalar()


def until(predicate, timeout=30):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError("replay condition timed out")


def latest(engine, mmsi):
    with engine.connect() as conn:
        row = conn.execute(
            text("select recorded_at, lat from ships.latest_positions where mmsi = :m"),
            {"m": mmsi},
        ).one()
    return tuple(row)


def historical(engine):
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "select lat_bin, lon_bin, count from ships.heat_cells_historical order by 1, 2"
            )
        ).all()
    return [tuple(row) for row in rows]


def position(mmsi, at, lat, lon, speed, name="", aisstream_time=False):
    stamp = (
        at.strftime("%Y-%m-%d %H:%M:%S.%f000 +0000 UTC")
        if aisstream_time
        else at.isoformat()
    )
    return json.dumps(
        {
            "MessageType": "PositionReport",
            "MetaData": {"MMSI": mmsi, "ShipName": name, "time_utc": stamp},
            "Message": {
                "PositionReport": {
                    "Latitude": lat,
                    "Longitude": lon,
                    "Sog": speed,
                    "Cog": 271.5,
                    "TrueHeading": 270,
                    "NavigationalStatus": 0,
                }
            },
        }
    )


def static(mmsi, **fields):
    return json.dumps(
        {
            "MessageType": "ShipStaticData",
            "MetaData": {"MMSI": mmsi, "ShipName": ""},
            "Message": {"ShipStaticData": fields},
        }
    )


def products(engine, mmsis):
    request = Request({"type": "http", "headers": []})
    with Session(engine) as session:
        snapshot = json.loads(get_snapshot(request, session).body)
        for row in snapshot["vessels"]:
            del row["updated_at"]  # Each database stamps its own commit time.
        snapshot["vessels"].sort(key=lambda row: row["mmsi"])
        tracks = {
            mmsi: get_track(mmsi, request, Response(), session=session)
            for mmsi in mmsis
        }
        heat = json.loads(get_heat(session).body)
        heat["cells"].sort()
    return snapshot, tracks, heat


def test_replay_matches_hub(pg, monkeypatch, feed, writers):
    hub = create_engine(pg.url, connect_args=UTC_SESSION)
    factory, dsn = factory_database(pg, "ships_replay")
    monkeypatch.setattr("core.db.get_engine", lambda: hub)
    midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)

    # A day past the seven-day retention, seeded identically in both databases.
    expired = (midnight - timedelta(days=8)).date()
    noon = datetime(expired.year, expired.month, expired.day, 12, tzinfo=UTC)
    seeded = [
        {"mmsi": "201", "lat": 48.0, "lon": -123.0, "speed": 3.0, "at": noon},
        {
            "mmsi": "201",
            "lat": 48.02,
            "lon": -123.05,
            "speed": 0.0,
            "at": noon + timedelta(minutes=10),
        },
        {"mmsi": "202", "lat": 48.0, "lon": -123.0, "speed": 0.0, "at": noon},
        {"mmsi": "203", "lat": 48.0, "lon": -123.0, "speed": 1.5, "at": noon},
    ]
    for engine in (hub, factory):
        with engine.begin() as conn:
            conn.execute(text(create_partition_sql(expired)))
            conn.execute(
                text(
                    "insert into ships.positions (mmsi, lat, lon, speed, recorded_at)"
                    " values (:mmsi, :lat, :lon, :speed, :at)"
                ),
                seeded,
            )

    eta = midnight + timedelta(days=40)
    frames = [
        static(
            123,
            Name=" Ferry ",
            CallSign=" CF1 ",
            ImoNumber=9100001,
            Type=60,
            Destination=" SEATTLE ",
            Dimension={"A": 50, "B": 20, "C": 8, "D": 8},
            MaximumStaticDraught=4.5,
            Eta={"Month": eta.month, "Day": eta.day, "Hour": 12, "Minute": 60},
        ),
        # Yesterday's partition, then a duplicate the dedup drops.
        position(
            123, midnight - timedelta(seconds=2), 49.0, -123.0, 0, aisstream_time=True
        ),
        position(
            123, midnight - timedelta(seconds=2), 49.0, -123.0, 0, aisstream_time=True
        ),
        # Today's partition. The next report is older and arrives later.
        position(123, midnight + timedelta(seconds=3), 49.01, -123.0, 2),
        position(123, midnight + timedelta(seconds=1), 49.005, -123.0, 2),
        static(123, Name="", Destination="", Type=61),
        # Stationary reports: the time threshold keeps the second, drops the third.
        position(456, midnight - timedelta(minutes=10), 48.5, -123.2, 0, name=" Tug "),
        position(456, midnight - timedelta(minutes=4), 48.5, -123.2, 0),
        position(456, midnight - timedelta(minutes=3), 48.5, -123.2, 0),
    ]
    mmsis = ["123", "456", "201", "202", "203"]

    # The hub path: scheduled maintenance, then ingest, rollup and a retry.
    asyncio.run(partition_maintenance_handler(None))
    banked = historical(hub)
    assert banked, "the hub banked nothing from the expired day"
    with Session(hub) as session:
        for raw in frames:
            kind, row = parse_message(raw)
            persist_batch(
                session,
                [row] if kind == "position" else [],
                [row] if kind == "vessel" else [],
            )
    asyncio.run(heat_rollup_handler(None))
    asyncio.run(partition_maintenance_handler(None))
    assert historical(hub) == banked

    # The factory path: session start banks the expired day before it subscribes.
    writers(dsn, feed.url, retention=True)
    ws = feed.accept()
    assert historical(factory) == banked
    part = f"ships.positions_{expired:%Y%m%d}"
    assert scalar(factory, f"select to_regclass('{part}') is null")
    for raw in frames:
        ws.send(raw)
    # The writer applies every received frame before the closed feed ends its
    # session. The next session retries retention and refreshes heat.
    ws.close()
    feed.accept()

    assert historical(factory) == banked
    assert products(factory, mmsis) == products(hub, mmsis)
    recorded_at, _ = latest(factory, "123")
    assert recorded_at == midnight + timedelta(seconds=3)
    partitions = scalar(
        factory,
        "select string_agg(distinct c.relname, ',' order by c.relname)"
        " from ships.positions p join pg_class c on c.oid = p.tableoid"
        " where p.mmsi = '123'",
    )
    assert partitions == ",".join(
        f"positions_{day:%Y%m%d}" for day in (midnight - timedelta(days=1), midnight)
    )


def test_restart_during_write_keeps_history_and_latest(pg, feed, writers):
    factory, dsn = factory_database(pg, "ships_restart")
    base = datetime.now(UTC) - timedelta(hours=1)
    history = "select count(*) from ships.positions"
    leader = writers(dsn, feed.url)
    ws = feed.accept()
    ws.send(position(321, base, 48.0, -123.0, 3))
    until(lambda: scalar(factory, history) == 1)

    with factory.connect() as blocker:
        blocker.exec_driver_sql("lock table ships.positions in access exclusive mode")
        ws.send(position(321, base + timedelta(seconds=10), 48.01, -123.0, 3))
        until(
            lambda: (
                scalar(factory, "select count(*) from ships.inbox") == 1
                and scalar(factory, LOCK_WAIT) == 1
            )
        )
        standby = writers(dsn, feed.url)

        # Frames the stored report cannot reach the database while it is blocked.
        ignored = " " * 8192 + json.dumps(
            {"MessageType": "Ignored", "MetaData": {"MMSI": 999}}
        )
        backlog = 8192
        sent = []

        def flood():
            try:
                for _ in range(backlog):
                    ws.send(ignored)
                    sent.append(1)
            except (ConnectionClosed, OSError):
                pass  # The leader's socket closes when the test kills it.

        threading.Thread(target=flood, daemon=True).start()
        peak = 0
        for _ in range(40):
            status = Path(f"/proc/{leader.pid}/status").read_text()
            rss = next(
                line for line in status.splitlines() if line.startswith("VmRSS:")
            )
            peak = max(peak, int(rss.split()[1]))
            time.sleep(0.05)
        assert feed.sockets.empty(), "the standby opened a second socket"
        assert len(sent) < backlog, (
            "the leader kept reading while its write was blocked"
        )
        assert peak < 64 * 1024, f"leader RSS reached {peak} KiB"
        assert blocker.exec_driver_sql(history).scalar() == 1
        leader.kill()
        leader.wait()

    # The standby takes the lease and applies the spooled report before it subscribes.
    ws = feed.accept()
    assert scalar(factory, history) == 2
    assert scalar(factory, "select count(*) from ships.inbox") == 0
    assert latest(factory, "321") == (base + timedelta(seconds=10), 48.01)

    # An older report joins the history and keeps the newer latest position.
    ws.send(position(321, base + timedelta(seconds=5), 48.005, -123.0, 3))
    until(lambda: scalar(factory, history) == 3)
    assert latest(factory, "321") == (base + timedelta(seconds=10), 48.01)

    # SIGTERM commits the frames the writer already received.
    ws.send(position(321, base + timedelta(seconds=20), 48.02, -123.0, 3))
    time.sleep(0.5)
    standby.send_signal(signal.SIGTERM)
    assert standby.wait(timeout=20) == 0
    assert scalar(factory, history) == 4
    assert latest(factory, "321") == (base + timedelta(seconds=20), 48.02)


def test_writer_survives_default_rows_future_and_refused_reports(pg, feed, writers):
    factory, dsn = factory_database(pg, "ships_partitions")
    now = datetime.now(UTC)
    tomorrow = (now + timedelta(days=1)).replace(
        hour=6, minute=0, second=0, microsecond=0
    )
    # No day partition exists yet, so this row lands in positions_default.
    with factory.begin() as conn:
        conn.execute(
            text(
                "insert into ships.positions (mmsi, lat, lon, speed, recorded_at)"
                " values ('401', 48.0, -123.0, 0, :at)"
            ),
            {"at": tomorrow},
        )
    writers(dsn, feed.url)
    ws = feed.accept()
    assert scalar(factory, "select count(*) from ships.positions_default") == 0
    assert (
        scalar(
            factory,
            "select c.relname from ships.positions p join pg_class c on c.oid = p.tableoid"
            " where p.mmsi = '401'",
        )
        == f"positions_{tomorrow:%Y%m%d}"
    )

    ws.send(position(402, now + timedelta(days=1), 48.0, -123.0, 3))
    # PostgreSQL refuses a NUL byte in text. The writer drops only that frame.
    ws.send(
        position(404, now - timedelta(minutes=2), 48.0, -123.0, 3, name="bad\u0000")
    )
    ws.send(position(403, now - timedelta(minutes=1), 48.0, -123.0, 3))
    until(
        lambda: (
            scalar(factory, "select count(*) from ships.positions where mmsi = '403'")
            == 1
        )
    )
    assert (
        scalar(
            factory, "select count(*) from ships.positions where mmsi in ('402', '404')"
        )
        == 0
    )
    assert scalar(factory, "select count(*) from ships.inbox") == 0
