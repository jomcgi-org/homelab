"""Restore a synthetic dump with pgvector data as the factory owner login.

The target mirrors the factory database: grimoire_restore owns it, the
operator's superuser installed pgvector, and grants.sql ran before the
restore, as the grimoire-db-grants Job does after every sync.
"""

import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import time

import pytest

HERE = Path(__file__).resolve().parent
VECTOR_QUERY = (
    "SELECT extversion, nspname FROM pg_extension JOIN pg_namespace "
    "ON pg_namespace.oid = extnamespace WHERE extname = 'vector'"
)
SOURCE = """
CREATE EXTENSION vector;
CREATE SCHEMA grimoire;
CREATE TABLE grimoire.account (
  id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  subject text NOT NULL UNIQUE);
CREATE TABLE grimoire.campaign (
  id bigserial PRIMARY KEY,
  owner_id bigint NOT NULL REFERENCES grimoire.account (id),
  name text NOT NULL);
CREATE TABLE grimoire.campaign_member (
  campaign_id bigint REFERENCES grimoire.campaign (id) ON DELETE CASCADE,
  account_id bigint REFERENCES grimoire.account (id),
  role text NOT NULL,
  PRIMARY KEY (campaign_id, account_id));
CREATE TABLE grimoire.knowledge_chunk (
  id uuid PRIMARY KEY,
  campaign_id bigint REFERENCES grimoire.campaign (id),
  content text NOT NULL);
CREATE TABLE grimoire.embedding (
  chunk_id uuid PRIMARY KEY REFERENCES grimoire.knowledge_chunk (id) ON DELETE CASCADE,
  model text NOT NULL,
  embedding vector(3) NOT NULL);
INSERT INTO grimoire.account (subject)
  SELECT 'subject-' || i FROM generate_series(1, 4) i;
DELETE FROM grimoire.account WHERE subject = 'subject-4';
INSERT INTO grimoire.campaign (owner_id, name) VALUES (1, 'first'), (2, 'second');
INSERT INTO grimoire.campaign_member VALUES
  (1, 1, 'gm'), (1, 2, 'player'), (2, 2, 'gm'), (2, 3, 'player');
INSERT INTO grimoire.knowledge_chunk
  SELECT md5(i::text)::uuid, 1 + i % 2, 'synthetic chunk ' || i FROM generate_series(1, 40) i;
INSERT INTO grimoire.embedding
  SELECT md5(i::text)::uuid, 'test-model', ARRAY[sin(i), cos(i), i / 40.0]::vector(3)
  FROM generate_series(1, 40) i;
CREATE INDEX embedding_hnsw ON grimoire.embedding USING hnsw (embedding vector_cosine_ops);
CREATE INDEX embedding_ivfflat ON grimoire.embedding USING ivfflat (embedding vector_l2_ops)
  WITH (lists = 2);
"""
# A foreign key added NOT VALID over an orphan row. It stays NOT VALID in the
# target, so the restore must refuse the dump before it imports anything.
NOT_VALID_SOURCE = """
CREATE SCHEMA grimoire;
CREATE TABLE grimoire.account (id bigint PRIMARY KEY);
CREATE TABLE grimoire.note (account_id bigint);
INSERT INTO grimoire.note VALUES (99);
ALTER TABLE grimoire.note
  ADD FOREIGN KEY (account_id) REFERENCES grimoire.account (id) NOT VALID;
"""
ROLES = """
CREATE ROLE grimoire_restore LOGIN;
CREATE ROLE grimoire_api LOGIN NOINHERIT;
CREATE ROLE grimoire_worker LOGIN NOINHERIT;
"""
# Every object the restore makes belongs to grimoire_restore.
OWNED = (
    "SELECT (SELECT count(*) FROM pg_namespace WHERE nspowner = 'grimoire_restore'::regrole)"
    " + (SELECT count(*) FROM pg_class WHERE relowner = 'grimoire_restore'::regrole)"
)
NEAREST = (
    "SELECT chunk_id FROM grimoire.embedding ORDER BY embedding <=> '[1,0,0]' LIMIT 3"
)


def runfile(variable):
    return Path(os.environ[variable]).resolve()


def pg_paths():
    srcdir = Path(os.environ["TEST_SRCDIR"])
    for name in ("+postgres+postgres_test", "postgres_test"):
        root = srcdir / name
        binary = root / "usr/lib/postgresql/16/bin"
        if (binary / "pg_restore").exists():
            return binary, root / "usr/share/postgresql/16"
    pytest.fail("@postgres_test runfiles are missing", pytrace=False)


def as_nobody():
    if os.getuid() == 0:
        nobody = pwd.getpwnam("nobody")
        os.setgid(nobody.pw_gid)
        os.setuid(nobody.pw_uid)


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("pg")
    pg_bin, pg_share = pg_paths()
    share = tmp / "share"
    shutil.copytree(pg_share, share)
    for name, content in {
        "postgresql.conf.sample": "listen_addresses = '127.0.0.1'\n",
        "pg_hba.conf.sample": "host all all 127.0.0.1/32 trust\n",
        "pg_ident.conf.sample": "# test identity map\n",
    }.items():
        if not (share / name).exists():
            (share / name).write_text(content)
    data = tmp / "data"
    data.mkdir()
    if os.getuid() == 0:
        nobody = pwd.getpwnam("nobody")
        os.chown(data, nobody.pw_uid, nobody.pw_gid)
        for parent in (tmp, *tmp.parents):
            parent.chmod(parent.stat().st_mode | 0o111)
    subprocess.run(
        [pg_bin / "initdb", "-D", data, "-U", "test", "--no-locale", "-L", share],
        check=True,
        capture_output=True,
        preexec_fn=as_nobody,
    )
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    log = (tmp / "postgres.log").open("w+")
    postgres = subprocess.Popen(
        [pg_bin / "postgres", "-D", data, "-h", "127.0.0.1", "-p", str(port)]
        + ["-c", "unix_socket_directories=", "-c", "fsync=off"],
        stdout=log,
        stderr=subprocess.STDOUT,
        preexec_fn=as_nobody,
    )
    env = dict(
        os.environ,
        PATH=f"{pg_bin}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        PGHOST="127.0.0.1",
        PGPORT=str(port),
        PGUSER="test",
        PGDATABASE="postgres",
        TMPDIR=str(tmp),
    )
    deadline = time.monotonic() + 30
    while subprocess.run(["pg_isready"], env=env, capture_output=True).returncode:
        if postgres.poll() is not None or time.monotonic() > deadline:
            log.seek(0)
            pytest.fail(log.read(), pytrace=False)
        time.sleep(0.2)
    yield env, tmp
    postgres.terminate()
    postgres.wait(timeout=30)
    log.close()


def psql(env, database, *args, user="test", check=True):
    result = subprocess.run(
        ["psql", "-XAtq", "-v", "ON_ERROR_STOP=1", "-d", database, *args],
        env=dict(env, PGUSER=user),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if check and result.returncode:
        pytest.fail(result.stdout + result.stderr, pytrace=False)
    return result


def grants(env, database):
    # The same flags as the grimoire-db-grants Job.
    psql(
        env,
        database,
        "--single-transaction",
        "-f",
        runfile("GRANTS_SQL"),
        user="grimoire_restore",
    )


def create_target(env, database):
    # The operator creates the database and pgvector.
    psql(env, "postgres", "-c", f"CREATE DATABASE {database} OWNER grimoire_restore")
    psql(
        env,
        database,
        "-c",
        "CREATE EXTENSION vector; REVOKE CREATE ON SCHEMA public FROM PUBLIC",
    )
    # The PostSync Job runs before any restore, against the empty database.
    grants(env, database)


def dump(env, database, path):
    subprocess.run(["pg_dump", "-Fc", "-d", database, "-f", path], env=env, check=True)


def inventory(env, database):
    return psql(env, database, "-f", runfile("INVENTORY_SQL")).stdout


def restore(env, private_dump, metadata, database="grimoire"):
    return subprocess.run(
        ["bash", runfile("RESTORE_SH")],
        env=dict(
            env,
            PGUSER="grimoire_restore",
            PGDATABASE=database,
            PRIVATE_DUMP=str(private_dump),
            VECTOR_METADATA=str(metadata),
        ),
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.fixture(scope="module")
def restored(server):
    env, tmp = server
    psql(env, "postgres", "-c", "CREATE DATABASE source")
    psql(env, "source", "-c", SOURCE)
    dump(env, "source", tmp / "grimoire.dump")
    metadata = tmp / "vector.metadata"
    metadata.write_text(psql(env, "source", "-c", VECTOR_QUERY).stdout)
    psql(env, "postgres", "-c", ROLES)
    create_target(env, "grimoire")
    result = restore(env, tmp / "grimoire.dump", metadata)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "5 valid foreign keys and 2 valid pgvector indexes" in result.stdout
    # Before any test writes to the target.
    inventories = (inventory(env, "source"), inventory(env, "grimoire"))
    return env, tmp, metadata, inventories


def test_restore_matches_the_source_inventory(restored):
    source, target = restored[3]
    assert "grimoire.campaign_member" in source
    assert "USING hnsw" in source and "USING ivfflat" in source
    assert target == source


def test_identity_relationships_are_enforced(restored):
    env = restored[0]
    pairs = (
        "SELECT a.subject, c.name, m.role FROM grimoire.campaign_member m "
        "JOIN grimoire.account a ON a.id = m.account_id "
        "JOIN grimoire.campaign c ON c.id = m.campaign_id ORDER BY 1, 2"
    )
    assert (
        psql(env, "grimoire", "-c", pairs).stdout
        == psql(env, "source", "-c", pairs).stdout
    )
    orphan = psql(
        env,
        "grimoire",
        "-c",
        "INSERT INTO grimoire.campaign_member VALUES (1, 99, 'player')",
        user="grimoire_api",
        check=False,
    )
    assert orphan.returncode != 0
    assert "violates foreign key constraint" in orphan.stderr


def test_vector_search_uses_the_restored_index(restored):
    env = restored[0]
    # The index scans every row of this small table, so it returns the exact
    # neighbours.
    indexed = "SET enable_seqscan = off; SET hnsw.ef_search = 100; "
    plan = psql(
        env,
        "grimoire",
        "-c",
        indexed + "EXPLAIN (COSTS OFF) " + NEAREST,
        user="grimoire_worker",
    ).stdout
    assert "Index Scan using embedding_hnsw" in plan
    nearest = psql(env, "grimoire", "-c", indexed + NEAREST, user="grimoire_worker")
    exact = psql(env, "source", "-c", "SET enable_indexscan = off; " + NEAREST)
    assert nearest.stdout == exact.stdout
    assert len(nearest.stdout.split()) == 3


def test_runtime_logins_get_rows_but_no_ddl(restored):
    env = restored[0]
    for login in ("grimoire_api", "grimoire_worker"):
        psql(
            env,
            "grimoire",
            "-c",
            f"INSERT INTO grimoire.account (subject) VALUES ('{login}') RETURNING id",
            user=login,
        )
        ddl = psql(
            env,
            "grimoire",
            "-c",
            "CREATE TABLE grimoire.extra (id int)",
            user=login,
            check=False,
        )
        assert "permission denied for schema grimoire" in ddl.stderr


def test_grants_replace_a_hand_grant(restored):
    env = restored[0]
    has_truncate = (
        "SELECT has_table_privilege('grimoire_api', 'grimoire.account', 'TRUNCATE')"
    )
    psql(
        env,
        "grimoire",
        "-c",
        "GRANT TRUNCATE ON grimoire.account TO grimoire_api",
        user="grimoire_restore",
    )
    assert psql(env, "grimoire", "-c", has_truncate).stdout.strip() == "t"
    grants(env, "grimoire")
    assert psql(env, "grimoire", "-c", has_truncate).stdout.strip() == "f"


def test_restore_refuses_a_database_with_tables(restored):
    env, tmp, metadata, _ = restored
    result = restore(env, tmp / "grimoire.dump", metadata)
    assert result.returncode != 0
    assert "restore only into an empty database" in result.stderr


def test_restore_refuses_a_not_valid_key_and_a_retry_succeeds(restored):
    env, tmp, metadata, (source, _) = restored
    psql(env, "postgres", "-c", "CREATE DATABASE not_valid")
    psql(env, "not_valid", "-c", NOT_VALID_SOURCE)
    dump(env, "not_valid", tmp / "not-valid.dump")
    create_target(env, "retry")
    result = restore(env, tmp / "not-valid.dump", metadata, "retry")
    assert result.returncode != 0
    assert "NOT VALID foreign keys in the dump: 1." in result.stderr
    assert psql(env, "retry", "-c", OWNED).stdout.strip() == "0"
    result = restore(env, tmp / "grimoire.dump", metadata, "retry")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "5 valid foreign keys and 2 valid pgvector indexes" in result.stdout
    assert inventory(env, "retry") == source


def test_restore_refuses_other_pgvector_metadata(restored):
    env, tmp, _, _ = restored
    other = tmp / "other.metadata"
    other.write_text("0.0.1|public\n")
    result = restore(env, tmp / "grimoire.dump", other)
    assert result.returncode != 0
    assert "differ" in result.stdout + result.stderr
