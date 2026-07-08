"""Shared fixtures for monolith e2e tests.

Manages a real PostgreSQL 16 + pgvector instance per test session,
with per-test SAVEPOINT rollback for isolation.
"""

from __future__ import annotations

import hashlib
import logging
import os
import pwd
import random
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import event, text
from sqlmodel import Session, create_engine

logger = logging.getLogger(__name__)


def pytest_configure(config):
    """Set asyncio_mode and register custom markers."""
    config.option.asyncio_mode = "auto"
    config.addinivalue_line(
        "markers", "covers_route(path, method): marks test as covering an API route"
    )
    config.addinivalue_line(
        "markers", "covers_page(path): marks test as covering a frontend page"
    )
    config.addinivalue_line(
        "markers", "covers_public(name): marks test as covering a public function"
    )


# ---------------------------------------------------------------------------
# Suppress env vars BEFORE importing app modules (module-level side effects)
# ---------------------------------------------------------------------------
os.environ.pop("STATIC_DIR", None)
os.environ.pop("DISCORD_BOT_TOKEN", None)
os.environ.pop("ICAL_FEED_URL", None)
# Ensure libpq uses UTF-8 client encoding — without this, psycopg-binary
# may return the PG version string as bytes, which triggers a TypeError in
# SQLAlchemy's version detection regex.
os.environ.setdefault("PGCLIENTENCODING", "UTF8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class PgInfo:
    """Connection info for the test PostgreSQL instance."""

    url: str
    port: int


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pg_preexec() -> None:
    """Drop root privileges in the child process before running PG binaries.

    PostgreSQL refuses to run as root. BuildBuddy runs tests as root,
    so we demote to the ``nobody`` user after fork but before the binary runs.
    """
    if os.getuid() != 0:
        return
    nobody = pwd.getpwnam("nobody")
    os.setgid(nobody.pw_gid)
    os.setuid(nobody.pw_uid)


def _ensure_sample_configs(pg_share: Path) -> None:
    """Create minimal .sample config files if missing from OCI extraction.

    initdb requires postgresql.conf.sample, pg_hba.conf.sample, and
    pg_ident.conf.sample as templates. These may be absent from the
    extracted OCI layers (Docker layer whiteout or packaging variation).
    """
    samples = {
        "postgresql.conf.sample": (
            "# Minimal postgresql.conf for testing\n"
            "listen_addresses = '127.0.0.1'\n"
            "max_connections = 100\n"
            "shared_buffers = 128MB\n"
            "dynamic_shared_memory_type = posix\n"
            "log_timezone = 'UTC'\n"
            "datestyle = 'iso, mdy'\n"
            "timezone = 'UTC'\n"
            "lc_messages = 'C'\n"
            "lc_monetary = 'C'\n"
            "lc_numeric = 'C'\n"
            "lc_time = 'C'\n"
            "default_text_search_config = 'pg_catalog.english'\n"
        ),
        "pg_hba.conf.sample": (
            "# TYPE  DATABASE  USER  ADDRESS  METHOD\n"
            "local   all       all             trust\n"
            "host    all       all   127.0.0.1/32  trust\n"
            "host    all       all   ::1/128       trust\n"
        ),
        "pg_ident.conf.sample": "# MAPNAME  SYSTEM-USERNAME  PG-USERNAME\n",
    }
    for filename, content in samples.items():
        path = pg_share / filename
        if not path.exists():
            path.write_text(content)


def _find_pg_root() -> Path:
    """Locate the extracted PostgreSQL binaries from Bazel runfiles.

    The OCI extraction preserves Debian filesystem paths, so the postgres
    binary is at ``usr/lib/postgresql/16/bin/postgres`` within the repo.
    Bzlmod repo names use ``+postgres+postgres_test`` prefix.
    """
    srcdir = os.environ.get("TEST_SRCDIR", "")
    # Bazel bzlmod: external repos live at the runfiles root (not under _main/external/)
    candidates = [
        Path(srcdir) / "+postgres+postgres_test",
        Path(srcdir) / "postgres_test",
        Path(srcdir) / "_main" / "external" / "+postgres+postgres_test",
        Path(srcdir) / "_main" / "external" / "postgres_test",
    ]
    for candidate in candidates:
        # Check Debian-style path first (OCI extraction preserves it)
        pg16_root = candidate / "usr" / "lib" / "postgresql" / "16"
        if (pg16_root / "bin" / "postgres").exists():
            return candidate
        # Also check flat layout (in case extraction is restructured later)
        if (candidate / "bin" / "postgres").exists():
            return candidate
    # Diagnostics: list what's actually in the runfiles to help debug path issues
    diag_lines = []
    base = Path(srcdir)
    for search_dir in [base, base / "_main", base / "_main" / "external"]:
        if search_dir.is_dir():
            entries = sorted(search_dir.iterdir())[:20]
            diag_lines.append(f"  {search_dir}: {[e.name for e in entries]}")
    for candidate in candidates:
        if candidate.is_dir():
            entries = sorted(candidate.iterdir())[:10]
            diag_lines.append(f"  {candidate}: {[e.name for e in entries]}")
    diag = "\n".join(diag_lines) if diag_lines else "  (no directories found)"
    raise FileNotFoundError(
        f"Could not find postgres_test binaries under TEST_SRCDIR={srcdir!r}.\n"
        f"Searched: {[str(c) for c in candidates]}\n"
        f"Directory contents:\n{diag}"
    )


def _find_migrations_dir() -> Path:
    """Locate migration SQL files from Bazel runfiles."""
    srcdir = os.environ.get("TEST_SRCDIR", "")
    candidate = (
        Path(srcdir) / "_main" / "projects" / "monolith" / "chart" / "migrations"
    )
    if candidate.is_dir():
        return candidate
    raise FileNotFoundError(
        f"Could not find migrations dir at {candidate}. TEST_SRCDIR={srcdir!r}"
    )


# ---------------------------------------------------------------------------
# Session-scoped: real PostgreSQL
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def pg(tmp_path_factory):
    """Start a real PostgreSQL 16 + pgvector instance for the test session."""
    pg_root = _find_pg_root()
    # OCI extraction preserves Debian paths; check for that first
    pg16_root = pg_root / "usr" / "lib" / "postgresql" / "16"
    if (pg16_root / "bin" / "postgres").exists():
        pg_bin = pg16_root / "bin"
        pg_lib = pg_root / "usr" / "lib"  # shared libs are here
        pg_share = pg_root / "usr" / "share" / "postgresql" / "16"
    else:
        pg_bin = pg_root / "bin"
        pg_lib = pg_root / "lib"
        pg_share = pg_root / "share"

    datadir = tmp_path_factory.mktemp("pgdata")
    port = _find_free_port()

    env = os.environ.copy()

    # DO NOT set LD_LIBRARY_PATH with the Debian libraries here.
    # The CI host has an older glibc than Debian Bookworm, so setting
    # LD_LIBRARY_PATH would poison ALL child processes (including /bin/sh
    # used by the wrapper scripts), causing segfaults. The PG binary
    # wrappers use ld-linux --library-path internally for full isolation.
    # On macOS (no wrappers), set DYLD_LIBRARY_PATH as fallback.
    pg_lib_internal = pg_lib / "postgresql" / "16" / "lib"
    pg_arch_lib = pg_lib / "x86_64-linux-gnu"
    lib_path_str = f"{pg_arch_lib}:{pg_lib_internal}:{pg_lib}"
    if sys.platform == "darwin":
        existing_dyld = env.get("DYLD_LIBRARY_PATH", "")
        env["DYLD_LIBRARY_PATH"] = (
            f"{lib_path_str}:{existing_dyld}" if existing_dyld else lib_path_str
        )

    # --- ensure initdb template files exist ---
    # The OCI image layers may not include the .sample config templates
    # that initdb requires. Create minimal versions if missing.
    _ensure_sample_configs(pg_share)

    # --- initdb ---
    # If running as root (BuildBuddy CI), ensure the demoted user (nobody)
    # can read PG files and write to the datadir.
    if os.getuid() == 0:
        nobody = pwd.getpwnam("nobody")
        # chown the datadir AND parent dirs so nobody can traverse the path
        os.chown(datadir, nobody.pw_uid, nobody.pw_gid)
        # Make pytest temp base traversable (e.g. /tmp/pytest-of-root/pytest-0)
        for parent in [datadir.parent, datadir.parent.parent]:
            try:
                parent.chmod(parent.stat().st_mode | 0o711)
            except OSError:
                pass
        # Make PG runfiles readable+traversable by nobody
        subprocess.run(["chmod", "-R", "a+rX", str(pg_root)], check=True, timeout=30)

    initdb_result = subprocess.run(  # nosemgrep: subprocess-run-no-timeout
        [
            str(pg_bin / "initdb"),
            "-D",
            str(datadir),
            "--no-locale",
            "-U",
            "test",
            "-L",
            str(pg_share),
        ],
        env=env,
        capture_output=True,
        preexec_fn=_pg_preexec,
    )
    if initdb_result.returncode != 0:
        raise RuntimeError(
            f"initdb failed (rc={initdb_result.returncode}).\n"
            f"  pg_bin: {pg_bin}\n"
            f"  stdout: {initdb_result.stdout.decode(errors='replace')}\n"
            f"  stderr: {initdb_result.stderr.decode(errors='replace')}"
        )

    # --- start postgres ---
    proc = subprocess.Popen(
        [
            str(pg_bin / "postgres"),
            "-D",
            str(datadir),
            "-p",
            str(port),
            "-k",
            str(datadir),  # unix socket dir
            "-c",
            f"dynamic_library_path={lib_path_str}",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        preexec_fn=_pg_preexec,
    )

    # --- wait for ready ---
    pg_isready = pg_bin / "pg_isready"
    deadline = time.monotonic() + 6.0
    ready = False
    while time.monotonic() < deadline:
        result = subprocess.run(  # nosemgrep: subprocess-run-no-timeout
            [str(pg_isready), "-h", "127.0.0.1", "-p", str(port), "-U", "test"],
            env=env,
            capture_output=True,
            preexec_fn=_pg_preexec,
        )
        if result.returncode == 0:
            ready = True
            break
        time.sleep(0.2)

    if not ready:
        proc.terminate()
        proc.wait(timeout=5)
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        raise RuntimeError(
            f"PostgreSQL failed to become ready within 6s on port {port}.\n"
            f"stderr: {stderr}"
        )

    # --- create database and install pgvector ---
    base_url = f"postgresql+psycopg://test@127.0.0.1:{port}"
    engine = create_engine(f"{base_url}/postgres")
    with engine.connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(text("CREATE DATABASE monolith"))
    engine.dispose()

    monolith_url = f"{base_url}/monolith"
    engine = create_engine(monolith_url)
    with engine.connect() as conn:
        conn = conn.execution_options(isolation_level="AUTOCOMMIT")
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    engine.dispose()

    # --- apply migrations ---
    migrations_dir = _find_migrations_dir()
    sql_files = sorted(migrations_dir.glob("*.sql"))
    engine = create_engine(monolith_url)
    with engine.begin() as conn:
        for sql_file in sql_files:
            conn.execute(text(sql_file.read_text()))
    engine.dispose()

    yield PgInfo(url=monolith_url, port=port)

    # --- teardown ---
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Function-scoped: SAVEPOINT session
# ---------------------------------------------------------------------------


@pytest.fixture()
def session(pg):
    """Per-test session with SAVEPOINT rollback for isolation."""
    engine = create_engine(pg.url)
    conn = engine.connect()
    txn = conn.begin()
    conn.begin_nested()  # SAVEPOINT

    sess = Session(bind=conn)

    @event.listens_for(sess, "after_transaction_end")
    def restart_savepoint(session, transaction):
        if transaction.nested and not transaction._parent.nested:
            session.begin_nested()

    yield sess

    sess.close()
    txn.rollback()
    conn.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# Function-scoped: FastAPI TestClient
# ---------------------------------------------------------------------------


def _make_create_task_patcher():
    """Return a side_effect function that closes coroutines instead of scheduling them."""

    def capture_create_task(coro, **kwargs):
        if hasattr(coro, "close"):
            coro.close()
        return MagicMock()

    return capture_create_task


@pytest.fixture()
def client(session):
    """TestClient with DB override and suppressed background tasks."""
    from app.db import get_session  # noqa: E402
    from app.main import app  # noqa: E402

    def get_session_override():
        yield session

    app.dependency_overrides[get_session] = get_session_override

    with patch("asyncio.create_task", side_effect=_make_create_task_patcher()):
        from fastapi.testclient import TestClient

        yield TestClient(app, raise_server_exceptions=False)

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Function-scoped: mock EmbeddingClient
# ---------------------------------------------------------------------------


def deterministic_embedding(text: str) -> list[float]:
    """Hash-based deterministic 1024-dim unit vector."""
    h = hashlib.sha256(text.encode()).digest()
    rng = random.Random(int.from_bytes(h[:8], "big"))
    vec = [rng.gauss(0, 1) for _ in range(1024)]
    norm = sum(x * x for x in vec) ** 0.5
    return [x / norm for x in vec]


@pytest.fixture()
def embed_client():
    """Mock EmbeddingClient with deterministic embeddings."""
    from shared.embedding import EmbeddingClient

    mock = MagicMock(spec=EmbeddingClient)
    mock.embed = AsyncMock(side_effect=deterministic_embedding)
    mock.embed_batch = AsyncMock(
        side_effect=lambda texts: [deterministic_embedding(t) for t in texts]
    )
    return mock


# ---------------------------------------------------------------------------
# Function-scoped: MessageStore
# ---------------------------------------------------------------------------


@pytest.fixture()
def store(session, embed_client):
    """MessageStore wired to the SAVEPOINT session and mock embeddings."""
    from chat.store import MessageStore

    return MessageStore(session=session, embed_client=embed_client)


# ---------------------------------------------------------------------------
# Session-scoped: live FastAPI server (uvicorn on a random port)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def live_server(pg):
    """Start a real uvicorn server backed by the test PostgreSQL.

    Yields the base URL (e.g. ``http://127.0.0.1:12345``).

    The server runs in a daemon thread with its own event loop.
    Background tasks (scheduler, calendar poll, Discord bot) are
    suppressed via env vars set before importing the app module.
    """
    port = _find_free_port()

    # Convert psycopg URL back to plain postgresql:// for the app's db.py,
    # which does its own scheme rewrite.
    raw_db_url = pg.url.replace("postgresql+psycopg://", "postgresql://", 1)

    # Build env overrides for the subprocess-like in-process server.
    # These are set before importing app.main so module-level reads pick
    # them up.
    os.environ["DATABASE_URL"] = raw_db_url
    os.environ.pop("STATIC_DIR", None)
    os.environ.pop("DISCORD_BOT_TOKEN", None)
    os.environ.pop("ICAL_FEED_URL", None)

    # Clear the cached engine so the app picks up the new DATABASE_URL.
    from app.db import get_engine

    get_engine.cache_clear()

    import uvicorn

    # Create a fresh app instance rather than reusing the module-level
    # singleton — this avoids state leaks from TestClient-based tests.
    # We import the module to trigger route registration, then grab the app.
    from app.main import app

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        # Disable lifespan to suppress background tasks (scheduler,
        # calendar poll, Discord bot) — they're irrelevant for HTTP tests.
        lifespan="off",
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for the server to accept connections
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base_url}/healthz", timeout=1.0)
            if r.status_code == 200:
                break
        except httpx.ConnectError:
            pass
        time.sleep(0.1)
    else:
        raise RuntimeError(
            f"Live FastAPI server failed to start within 10s on port {port}"
        )

    logger.info("Live server ready at %s", base_url)
    yield base_url

    # Teardown
    server.should_exit = True
    thread.join(timeout=5)
    get_engine.cache_clear()


# ---------------------------------------------------------------------------
# Session-scoped: SvelteKit frontend server (Node.js)
# ---------------------------------------------------------------------------


def _find_frontend_build() -> Path:
    """Locate the SvelteKit build output from Bazel runfiles."""
    srcdir = os.environ.get("TEST_SRCDIR", "")
    candidate = (
        Path(srcdir)
        / "_main"
        / "projects"
        / "monolith"
        / "frontend_dist"
        / "projects"
        / "monolith"
        / "frontend"
        / "build"
    )
    if candidate.is_dir():
        return candidate
    # Fallback: try without the exec_filegroup nesting
    candidate2 = Path(srcdir) / "_main" / "projects" / "monolith" / "frontend" / "build"
    if candidate2.is_dir():
        return candidate2
    raise FileNotFoundError(
        f"Could not find frontend build dir. Searched:\n"
        f"  {candidate}\n  {candidate2}\n"
        f"TEST_SRCDIR={srcdir!r}"
    )


def _find_node_binary() -> str:
    """Find the Node.js binary, preferring Bazel runfiles over system PATH."""
    import shutil
    import platform

    srcdir = os.environ.get("TEST_SRCDIR", "")
    if srcdir:
        # Try Bazel-managed Node.js from rules_nodejs
        arch = "amd64" if platform.machine() in ("x86_64", "AMD64") else "arm64"
        system = "linux" if platform.system() == "Linux" else "darwin"
        candidate = (
            Path(srcdir)
            / "_main"
            / "external"
            / f"nodejs_{system}_{arch}"
            / "bin"
            / "node"
        )
        if candidate.exists():
            return str(candidate)
    # Fallback to system node
    node = shutil.which("node")
    if node:
        return node
    raise FileNotFoundError(
        "Could not find node binary in Bazel runfiles or system PATH"
    )


@pytest.fixture(scope="session")
def sveltekit_server(live_server):
    """Start a SvelteKit Node.js server backed by the live FastAPI server.

    Yields the base URL (e.g. ``http://127.0.0.1:PORT``).
    The SvelteKit server proxies API calls to the FastAPI live_server
    via the ``API_BASE`` environment variable.
    """
    build_dir = _find_frontend_build()
    node_bin = _find_node_binary()
    port = _find_free_port()

    env = os.environ.copy()
    env["PORT"] = str(port)
    env["HOST"] = "127.0.0.1"
    env["API_BASE"] = live_server
    env["ORIGIN"] = f"http://127.0.0.1:{port}"

    proc = subprocess.Popen(
        [node_bin, str(build_dir / "index.js")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(build_dir),
    )

    # Wait for the SvelteKit server to accept connections
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base_url}/private", timeout=1.0, follow_redirects=True)
            if r.status_code in (200, 302, 303):
                break
        except httpx.ConnectError:
            pass
        # Check if process died
        if proc.poll() is not None:
            stderr = proc.stderr.read().decode() if proc.stderr else ""
            raise RuntimeError(
                f"SvelteKit server exited with code {proc.returncode}.\n"
                f"stderr: {stderr}"
            )
        time.sleep(0.2)
    else:
        proc.terminate()
        proc.wait(timeout=5)
        stderr = proc.stderr.read().decode() if proc.stderr else ""
        raise RuntimeError(
            f"SvelteKit server failed to start within 15s on port {port}.\n"
            f"stderr: {stderr}"
        )

    logger.info("SvelteKit server ready at %s", base_url)
    yield base_url

    # Teardown
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


# ---------------------------------------------------------------------------
# Session-scoped: live server with fake embedding client
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def live_server_with_fake_embedding(live_server):
    """Override the knowledge router's embedding dependency for the live server.

    The live FastAPI instance reads from the ``app`` module-level singleton
    imported inside ``live_server``. Registering an override on that instance
    is enough to route every ``/api/knowledge/search`` request through the
    deterministic fake.
    """
    from unittest.mock import AsyncMock

    from app.main import app  # noqa: E402
    from knowledge.router import get_embedding_client  # noqa: E402

    fake = AsyncMock()
    fake.embed = AsyncMock(side_effect=deterministic_embedding)

    app.dependency_overrides[get_embedding_client] = lambda: fake
    yield live_server
    app.dependency_overrides.pop(get_embedding_client, None)
