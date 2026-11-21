"""Run the Ships publisher against PostgreSQL and compare it with the hub API.

The factory database starts from the chart's bootstrap SQL. The publisher
writes to a local S3 stand-in. The hub's serving code reads the same
database, so each published product compares with the hub response.
"""

import json
import os
import subprocess
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

import pytest
from fastapi import Request, Response
from sqlmodel import Session, create_engine, text

from ships.router import get_heat, get_snapshot, get_track

# The test PostgreSQL has no zone files. It resolves GMT without them.
UTC_SESSION = {"options": "-c timezone=GMT"}


class Bucket(BaseHTTPRequestHandler):
    """One S3 bucket named ships, kept in memory."""

    objects = {}

    def log_message(self, *args):
        pass

    def _key(self):
        path = urlparse(self.path).path
        return path.removeprefix("/ships/") if path.startswith("/ships/") else None

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/ships" and parse_qs(url.query).get("list-type") == ["2"]:
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            items = "".join(
                f"<Contents><Key>{escape(k)}</Key><LastModified>{stamp}</LastModified></Contents>"
                for k in sorted(self.objects)
            )
            self._send(
                200,
                f"<ListBucketResult>{items}<IsTruncated>false</IsTruncated></ListBucketResult>".encode(),
            )
            return
        obj = self.objects.get(self._key())
        if obj is None:
            self._send(404, b"NoSuchKey")
        else:
            self._send(200, obj["body"])

    def do_PUT(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.objects[self._key()] = {
            "body": body,
            "cache": self.headers["Cache-Control"],
        }
        self._send(200, b"")

    def do_DELETE(self):
        self.objects.pop(self._key(), None)
        self._send(204, b"")

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def bucket():
    Bucket.objects = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), Bucket)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


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
        # The operator sets LOGIN on the managed role (templates/database.yaml)
        # as the superuser. The bootstrap SQL ends as role ships.
        conn.exec_driver_sql("RESET ROLE")
        conn.exec_driver_sql("ALTER ROLE ships_publisher LOGIN")
    superuser.dispose()
    engine = create_engine(
        f"postgresql+psycopg://ships@127.0.0.1:{pg.port}/{name}",
        connect_args=UTC_SESSION,
    )
    # The publisher connects as its read-only login, as in the chart.
    return (
        engine,
        f"postgres://ships_publisher@127.0.0.1:{pg.port}/{name}?sslmode=disable",
    )


SEED = """
insert into ships.vessels (mmsi, name, ship_type, destination, eta)
values ('111', 'ALPHA', 70, 'SEATTLE', '2026-11-21 12:00:00+00');
insert into ships.positions (mmsi, lat, lon, speed, course, heading, nav_status, ship_name, recorded_at) values
 ('111', 48.5, -123.25, 12.5, 271.5, 270, 0, null, now() - interval '1 minute' - interval '123 microseconds'),
 ('111', 48.4, -123.1, 11, 270, 269, 0, null, now() - interval '11 minutes'),
 ('222', 47.6, -122.4, null, null, null, null, 'BRAVO', now() - interval '3 hours');
-- Older than the publisher's margin, so a later run reuses their tracks.
insert into ships.latest_positions (mmsi, lat, lon, speed, course, heading, nav_status, ship_name, recorded_at,
 first_seen_at_location, updated_at)
 select distinct on (mmsi) mmsi, lat, lon, speed, course, heading, nav_status, ship_name, recorded_at, recorded_at,
 now() - interval '10 minutes'
 from ships.positions order by mmsi, recorded_at desc;
-- A vessel whose history expired: the hub returns an empty track.
insert into ships.latest_positions (mmsi, lat, lon, recorded_at, updated_at)
values ('333', 49, -124, '2026-01-01 00:00:00+00', '2026-01-01 00:00:00+00');
insert into ships.heat_cells (lat_bin, lon_bin, count) values (9600, -16400, 3), (9601, -16400, 120);
insert into ships.heat_cells_historical (lat_bin, lon_bin, count) values (9600, -16400, 40), (9700, -16500, 2);
"""

REPORT = """
insert into ships.positions (mmsi, lat, lon, speed, recorded_at) values ('222', 47.7, -122.5, 9.5, now());
update ships.latest_positions set lat = 47.7, lon = -122.5, speed = 9.5, recorded_at = now(), updated_at = now()
 where mmsi = '222';
"""


def publish(dsn, endpoint):
    env = dict(
        os.environ,
        DATABASE_URL=dsn,
        AWS_ENDPOINT_URL=endpoint,
        AWS_REGION="garage",
        AWS_ACCESS_KEY_ID="fixture",
        AWS_SECRET_ACCESS_KEY="fixture",
        BUCKET="ships",
    )
    binary = os.path.abspath(os.environ["SHIPS_BINARY"])
    result = subprocess.run(
        [binary, "publish"], env=env, capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    return result.stderr


def published():
    def read(key):
        return json.loads(Bucket.objects[key]["body"])

    manifest = read("latest.json")
    snapshot = read(manifest["snapshot"])
    tracks = {v["mmsi"]: v["track"] and read(v["track"]) for v in snapshot["vessels"]}
    return manifest, snapshot, tracks, read(manifest["heat"])


def hub(engine, mmsis):
    request = Request({"type": "http", "headers": []})
    with Session(engine) as session:
        snapshot = json.loads(get_snapshot(request, session).body)
        tracks = {m: get_track(m, request, Response(), session=session) for m in mmsis}
        heat = json.loads(get_heat(session).body)
    return snapshot, tracks, heat


def assert_matches_hub(engine):
    manifest, snapshot, tracks, heat = published()
    hub_snapshot, hub_tracks, hub_heat = hub(engine, list(tracks))
    assert manifest["schema_version"] == snapshot["schema_version"] == 1
    assert snapshot["generated_at"] == manifest["generated_at"]
    vessels = [
        {k: v for k, v in row.items() if k != "track"} for row in snapshot["vessels"]
    ]
    assert vessels == sorted(hub_snapshot["vessels"], key=lambda row: row["mmsi"])
    assert snapshot["count"] == hub_snapshot["count"] == 3
    for mmsi, track in tracks.items():
        if hub_tracks[mmsi]["count"] == 0:
            # No retained history: the vessel has no track key.
            assert track is None
            continue
        assert track["schema_version"] == 1
        del track["schema_version"]
        assert track == json.loads(json.dumps(hub_tracks[mmsi]))
    assert heat.pop("schema_version") == 1
    assert heat.pop("computed_at") is not None
    heat["cells"].sort()
    hub_heat["cells"].sort()
    assert heat == hub_heat
    return manifest, snapshot


def test_publisher_matches_hub(pg, bucket):
    engine, dsn = factory_database(pg, "ships_publish")
    with engine.begin() as conn:
        conn.exec_driver_sql(SEED)
    log = publish(dsn, bucket)
    assert "full=true" in log
    first, first_snapshot = assert_matches_hub(engine)
    keys = {key: obj["cache"] for key, obj in Bucket.objects.items()}
    assert keys.pop("latest.json") == "public, max-age=60"
    # The collector's state: the retirement time of each unnamed payload.
    assert keys.pop("publisher-state.json") == "no-store"
    assert set(keys.values()) == {"public, max-age=31536000, immutable"}

    # A new report for vessel 222 rebuilds only its track.
    with engine.begin() as conn:
        conn.exec_driver_sql(REPORT)
    log = publish(dsn, bucket)
    second, second_snapshot = assert_matches_hub(engine)
    # The first run of an hour reads every track.
    if first["generated_at"][:13] == second["generated_at"][:13]:
        assert "full=false" in log and "rebuilt=1 " in log
    else:
        assert "full=true" in log
    assert second["generated_at"] > first["generated_at"]
    assert second["heat"] == first["heat"]
    tracks = {v["mmsi"]: v["track"] for v in first_snapshot["vessels"]}
    changed = {v["mmsi"]: v["track"] for v in second_snapshot["vessels"]}
    assert changed["111"] == tracks["111"] and changed["222"] != tracks["222"]


# Each statement needs a privilege that ships_publisher lacks. Each runs in
# its own read-write transaction: the grants refuse it, not the READ ONLY
# mode of the publisher's own transaction.
DENIED = [
    "insert into ships.vessels (mmsi) values ('999')",
    "update ships.latest_positions set lat = 0",
    "delete from ships.positions",
    "truncate ships.heat_cells",
    "select * from ships.vessels for update",
    "lock table ships.heat_cells_historical in exclusive mode",
    "select * from ships.inbox",
    "lock table ships.ingest_lease in exclusive mode nowait",
    "delete from ships.inbox",
    "select nextval('ships.positions_id_seq')",
    "alter table ships.positions add column extra integer",
    "drop table ships.heat_cells_historical",
    "create table ships.extra (id integer)",
    "create table public.extra (id integer)",
    "create index on ships.vessels (name)",
    "create schema extra",
    "set role ships",
]


def test_publisher_login_cannot_write_or_alter(pg):
    engine, dsn = factory_database(pg, "ships_publisher_grants")
    with engine.begin() as conn:
        conn.exec_driver_sql(SEED)
        # A day partition that the ingester attaches later. A read through
        # the parent needs no grant on it.
        conn.exec_driver_sql(
            "CREATE TABLE ships.positions_20990101 (LIKE ships.positions);"
            "ALTER TABLE ships.positions ATTACH PARTITION ships.positions_20990101"
            " FOR VALUES FROM ('2099-01-01') TO ('2099-01-02');"
            "INSERT INTO ships.positions (mmsi, lat, lon, recorded_at)"
            " VALUES ('111', 48, -123, '2099-01-01 12:00:00+00')"
        )
    login = create_engine(dsn.replace("postgres://", "postgresql+psycopg://", 1))
    with login.connect() as conn:
        assert (
            conn.exec_driver_sql("select count(*) from ships.positions").scalar() == 4
        )
        for table in (
            "vessels",
            "latest_positions",
            "heat_cells",
            "heat_cells_historical",
        ):
            conn.exec_driver_sql(f"select * from ships.{table}").fetchall()
        conn.rollback()
        attributes = conn.exec_driver_sql(
            "select rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolreplication,"
            " rolbypassrls from pg_roles where rolname = current_user"
        ).one()
        assert not any(attributes)
        owned = conn.exec_driver_sql(
            "select count(*) from pg_class where relowner ="
            " (select oid from pg_roles where rolname = current_user)"
        ).scalar()
        assert owned == 0
        conn.rollback()
        for statement in DENIED:
            # SQLAlchemy wraps the driver error; its .orig carries the
            # SQLSTATE. Catching Exception avoids a direct sqlalchemy import.
            with pytest.raises(Exception) as refused:
                with conn.begin():
                    conn.exec_driver_sql(statement)
            assert refused.value.orig.sqlstate == "42501", statement
    # The publisher keeps its own lock while the owner holds the ingester lease.
    with engine.begin() as owner, login.connect() as publisher:
        owner.exec_driver_sql("lock table ships.ingest_lease in exclusive mode nowait")
        assert publisher.exec_driver_sql("select pg_try_advisory_lock(558, 1)").scalar()
        assert publisher.exec_driver_sql("select pg_advisory_unlock(558, 1)").scalar()
    login.dispose()
    # The owner still sees the seeded rows: nothing the login tried changed them.
    with engine.connect() as conn:
        assert conn.exec_driver_sql("select count(*) from ships.vessels").scalar() == 1
        assert (
            conn.exec_driver_sql("select count(*) from ships.positions").scalar() == 4
        )
    engine.dispose()


def test_maintenance_grants_the_login_on_an_older_database(pg):
    # HOME-516 created ships-db before the login existed, so its bootstrap
    # ran no grants. The ingester's maintenance runs as the owner and grants
    # the same SELECT once CloudNativePG creates the role.
    engine, dsn = factory_database(pg, "ships_publisher_maintenance")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "REVOKE USAGE ON SCHEMA ships FROM ships_publisher;"
            "REVOKE SELECT ON ALL TABLES IN SCHEMA ships FROM ships_publisher"
        )
    login = create_engine(dsn.replace("postgres://", "postgresql+psycopg://", 1))
    with login.connect() as conn:
        with pytest.raises(Exception) as refused:
            conn.exec_driver_sql("select * from ships.vessels")
        assert refused.value.orig.sqlstate == "42501"
    with engine.begin() as conn:
        # The ingester sets the retention flag, then runs the file as one
        # transaction. The file holds % in format() strings.
        conn.exec_driver_sql("select set_config('ships.retention', 'off', true)")
        conn.execution_options(no_parameters=True).exec_driver_sql(
            Path(os.environ["SHIPS_MAINTENANCE_SQL"]).read_text()
        )
    with login.connect() as conn:
        for table in (
            "vessels",
            "positions",
            "latest_positions",
            "heat_cells",
            "heat_cells_historical",
        ):
            conn.exec_driver_sql(f"select * from ships.{table}").fetchall()
        conn.rollback()
        with pytest.raises(Exception) as refused:
            with conn.begin():
                conn.exec_driver_sql("select * from ships.inbox")
        assert refused.value.orig.sqlstate == "42501"
    login.dispose()
    engine.dispose()
