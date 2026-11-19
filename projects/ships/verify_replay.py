"""Replay against two empty scratch databases using the hub's serving code.

Set HUB_TEST_URL, SHIPS_TEST_URL and SHIPS_BINARY. Run with projects/monolith
on PYTHONPATH and the monolith SQLModel, psycopg2 and websockets dependencies.
"""

import json
import os
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from queue import Queue
import subprocess
from threading import Thread
import time

from fastapi import Request, Response
from sqlalchemy import create_engine, text
from sqlmodel import Session
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

from ships.ais import parse_message
from ships.heat import rollup_insert_sql
from ships.router import get_heat, get_snapshot, get_track
from ships.store import persist_batch


def until(predicate):
    end = time.monotonic() + 15
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("replay condition timed out")


def products(db):
    request = Request({"type": "http", "headers": []})
    with Session(db) as session:
        snapshot = json.loads(get_snapshot(request, session).body)
        for row in snapshot["vessels"]:
            del row["updated_at"]  # Database commit clocks differ between runs.
        heat = json.loads(get_heat(session).body)
        heat["cells"].sort()
        return snapshot, get_track("123", request, Response(), session=session), heat


def main():
    hub = create_engine(
        os.environ["HUB_TEST_URL"].replace("postgresql:", "postgresql+psycopg2:", 1)
    )
    factory = create_engine(
        os.environ["SHIPS_TEST_URL"].replace("postgresql:", "postgresql+psycopg2:", 1)
    )
    migrations = Path(__file__).resolve().parents[1] / "monolith/chart/migrations"
    for db in (hub, factory):
        with db.begin() as conn:
            for path in sorted(migrations.glob("*ships*.sql")):
                conn.exec_driver_sql(path.read_text())

    midnight = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    def position(seconds, speed):
        return json.dumps(
            {
                "MetaData": {
                    "MMSI": 123,
                    "time_utc": (midnight + timedelta(seconds=seconds)).isoformat(),
                },
                "MessageType": "PositionReport",
                "Message": {
                    "PositionReport": {"Latitude": 49, "Longitude": -123, "Sog": speed}
                },
            }
        )

    static = json.dumps(
        {
            "MetaData": {"MMSI": 123},
            "MessageType": "ShipStaticData",
            "Message": {"ShipStaticData": {"Name": " Ferry ", "Type": 60}},
        }
    )
    frames = [static, position(-2, 0), position(-2, 0), position(3, 2), position(1, 2)]
    with Session(hub) as session:
        for raw in frames:
            kind, row = parse_message(raw)
            persist_batch(
                session,
                [row] if kind == "position" else [],
                [row] if kind == "vessel" else [],
            )
        session.execute(text(rollup_insert_sql(0.005, 0.0075, 1, 7)))
        session.commit()

    sockets = Queue()

    def connected(ws):
        ws.recv(timeout=10)
        sockets.put(ws)
        ws.wait_closed()

    with ExitStack() as stack:
        server = stack.enter_context(serve(connected, "127.0.0.1", 0))
        Thread(target=server.serve_forever, daemon=True).start()
        env = dict(
            os.environ,
            DATABASE_URL=os.environ["SHIPS_TEST_URL"],
            AISSTREAM_API_KEY="fixture",
            AISSTREAM_URL=f"ws://127.0.0.1:{server.socket.getsockname()[1]}",
        )

        def start():
            process = subprocess.Popen([os.environ["SHIPS_BINARY"]], env=env)
            stack.callback(lambda: (process.kill(), process.wait()))
            return process

        def scalar(query):
            with factory.connect() as conn:
                return conn.execute(text(query)).scalar()

        started = time.monotonic()
        leader = start()
        ws = sockets.get(timeout=15)
        for raw in frames[:-1]:
            ws.send(raw)
        until(lambda: scalar("select count(*) from ships.positions") == 2)
        with factory.connect() as blocker:
            blocker.exec_driver_sql("lock ships.positions in access exclusive mode")
            ws.send(frames[-1])
            until(
                lambda: (
                    scalar(
                        "select count(*) from pg_stat_activity where wait_event_type='Lock' and query like 'with added%'"
                    )
                    == 1
                )
            )
            start()

            def backlog(socket):
                try:
                    for _ in range(8192):
                        socket.send(" " * 8192 + frames[-1])
                except ConnectionClosed:
                    pass

            sender = Thread(target=backlog, args=(ws,), daemon=True)
            sender.start()
            samples = []
            for _ in range(25):
                status = Path(f"/proc/{leader.pid}/status").read_text()
                samples.append(
                    int(
                        next(
                            line.split()[1]
                            for line in status.splitlines()
                            if line.startswith("VmRSS:")
                        )
                    )
                )
                time.sleep(0.05)
            assert sockets.empty()
            rss = max(samples)
            assert rss < 256 * 1024, rss
            ticks = Path(f"/proc/{leader.pid}/stat").read_text().split()
            cpu_seconds = (int(ticks[13]) + int(ticks[14])) / os.sysconf("SC_CLK_TCK")
            cores = cpu_seconds / (time.monotonic() - started)
            leader.kill()
            leader.wait()
        ws = sockets.get(timeout=15)
        assert scalar("select count(*) from ships.positions") == 2
        ws.send(frames[-1])
        until(lambda: scalar("select count(*) from ships.positions") == 3)
        assert scalar("select count(distinct tableoid) from ships.positions") == 2
        ws.close()  # Reconnect runs the hourly heat refresh immediately.
        sockets.get(timeout=15)
        assert products(factory) == products(hub)
        print(
            f"Snapshot, track and heat match across restart and UTC rollover. peak sampled RSS {rss} KiB, mean CPU {cores:.4f} cores"
        )


if __name__ == "__main__":
    main()
