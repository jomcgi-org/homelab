"""Run the Ships publisher against PostgreSQL while another run holds its lock.

The publisher connects as a read-only login with the attributes and grants
that the chart gives ships_publisher. It takes the advisory lock (558, 1)
before it reads the bucket, and keeps it until it exits.
"""

import os
import subprocess
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

import pytest
from sqlmodel import create_engine, text

# The ships_publisher sessions that hold the publisher's lock.
HOLDERS = (
    "select count(*) from pg_locks l join pg_stat_activity a using (pid)"
    " where l.locktype = 'advisory' and l.granted and l.classid = 558"
    " and l.objid = 1 and l.objsubid = 2 and a.usename = 'ships_publisher'"
)
# The attributes and grants of the chart's login (templates/database.yaml).
PUBLISHER_ROLE = """
DO $$ BEGIN CREATE ROLE ships_publisher;
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
ALTER ROLE ships_publisher LOGIN NOINHERIT NOSUPERUSER CONNECTION LIMIT 2;
GRANT USAGE ON SCHEMA ships TO ships_publisher;
GRANT SELECT ON ships.vessels, ships.positions, ships.latest_positions,
    ships.heat_cells, ships.heat_cells_historical TO ships_publisher;
"""


class Bucket(BaseHTTPRequestHandler):
    """One S3 bucket named ships. It records each request with the number of
    publisher sessions that held the lock when the request arrived."""

    objects = {}
    requests = []
    superuser = None

    def log_message(self, *args):
        pass

    def _record(self):
        with self.superuser.connect() as conn:
            holders = conn.execute(text(HOLDERS)).scalar()
        self.requests.append((self.command, urlparse(self.path).path, holders))

    def do_GET(self):
        self._record()
        url = urlparse(self.path)
        if url.path == "/ships" and parse_qs(url.query).get("list-type") == ["2"]:
            stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            items = "".join(
                f"<Contents><Key>{escape(k)}</Key><LastModified>{stamp}</LastModified></Contents>"
                for k in sorted(self.objects)
            )
            body = f"<ListBucketResult>{items}<IsTruncated>false</IsTruncated></ListBucketResult>"
            self._send(200, body.encode())
            return
        body = self.objects.get(url.path.removeprefix("/ships/"))
        if body is None:
            self._send(404, b"NoSuchKey")
        else:
            self._send(200, body)

    def do_PUT(self):
        self._record()
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.objects[urlparse(self.path).path.removeprefix("/ships/")] = body
        self._send(200, b"")

    def do_DELETE(self):
        self._record()
        self.objects.pop(urlparse(self.path).path.removeprefix("/ships/"), None)
        self._send(204, b"")

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def database(pg):
    """Create the database the way the chart's bootstrap does, with the
    publisher's login. Return the superuser engine and the login's URLs."""
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
        conn.execute(text("CREATE DATABASE ships_lock OWNER ships"))
    admin.dispose()
    superuser = create_engine(f"{base}/ships_lock")
    with superuser.begin() as conn:
        conn.exec_driver_sql(Path(os.environ["SHIPS_BOOTSTRAP_SQL"]).read_text())
        # The bootstrap SQL ends as role ships.
        conn.exec_driver_sql("RESET ROLE")
        conn.exec_driver_sql(PUBLISHER_ROLE)
    login = f"ships_publisher@127.0.0.1:{pg.port}/ships_lock"
    yield (
        superuser,
        f"postgres://{login}?sslmode=disable",
        f"postgresql+psycopg://{login}",
    )
    superuser.dispose()


@pytest.fixture
def bucket(database):
    Bucket.objects, Bucket.requests, Bucket.superuser = {}, [], database[0]
    server = ThreadingHTTPServer(("127.0.0.1", 0), Bucket)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def publish(dsn, bucket):
    env = dict(
        os.environ,
        DATABASE_URL=dsn,
        AWS_ENDPOINT_URL=bucket,
        AWS_REGION="garage",
        BUCKET="ships",
        AWS_ACCESS_KEY_ID="test-key",
        AWS_SECRET_ACCESS_KEY="test-secret",
    )
    return subprocess.run(
        [os.path.abspath(os.environ["SHIPS_BINARY"]), "publish"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_second_run_does_nothing_while_the_lock_is_held(database, bucket):
    _, dsn, url = database
    other = create_engine(url)
    # Another run holds the lock on one of the login's two connections.
    with other.connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        assert conn.execute(text("select pg_try_advisory_lock(558, 1)")).scalar()
        run = publish(dsn, bucket)
        assert run.returncode == 0, run.stderr
        assert "another run holds the writer lock, nothing to do" in run.stderr
        assert Bucket.requests == []
        assert conn.execute(text("select pg_advisory_unlock(558, 1)")).scalar()
    other.dispose()

    # With the lock free, the run publishes and holds the lock for each request.
    run = publish(dsn, bucket)
    assert run.returncode == 0, run.stderr
    assert "latest.json" in Bucket.objects
    assert ("PUT", "/ships/latest.json", 1) in Bucket.requests
    assert all(holders == 1 for _, _, holders in Bucket.requests), Bucket.requests
