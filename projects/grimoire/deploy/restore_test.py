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
# An index predicate whose string holds a line that ends like a NOT VALID
# foreign key. The string is data, so the restore must accept the dump.
PREDICATE_SOURCE = """
CREATE EXTENSION vector;
CREATE SCHEMA grimoire;
CREATE TABLE grimoire.account (id bigint PRIMARY KEY);
CREATE TABLE grimoire.note (
  account_id bigint REFERENCES grimoire.account (id),
  body text);
INSERT INTO grimoire.account VALUES (1);
INSERT INTO grimoire.note VALUES (1, 'first');
CREATE INDEX note_marked ON grimoire.note (account_id) WHERE body <> 'first
 FOREIGN KEY x NOT VALID;
last';
"""
# The hub database: the Grimoire schema beside a schema that stays on the hub,
# with its own pgvector index and foreign key.
HUB_SOURCE = (
    SOURCE
    + """
CREATE SCHEMA knowledge;
CREATE TABLE knowledge.note (id bigserial PRIMARY KEY, body text NOT NULL);
CREATE TABLE knowledge.chunk (
  note_id bigint REFERENCES knowledge.note (id),
  embedding vector(3) NOT NULL);
INSERT INTO knowledge.note (body) VALUES ('stays on the hub');
INSERT INTO knowledge.chunk VALUES (1, '[1,0,0]');
CREATE INDEX chunk_hnsw ON knowledge.chunk USING hnsw (embedding vector_l2_ops);
"""
)
# Runs in place of pg_dump during the hub dump. It writes a row after the dump,
# before the inventory reads the source.
PG_DUMP_SHIM = """#!/bin/sh
"$REAL_PG_DUMP" "$@" || exit
exec psql -XAtq -d "$DATABASE_URL" -c \
  "INSERT INTO grimoire.campaign_member VALUES (2, 1, 'player')"
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


def restore(env, private_dump, metadata, database="grimoire", **extra):
    return subprocess.run(
        ["bash", runfile("RESTORE_SH")],
        env=dict(
            env,
            PGUSER="grimoire_restore",
            PGDATABASE=database,
            PRIVATE_DUMP=str(private_dump),
            VECTOR_METADATA=str(metadata),
            **extra,
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


def sequence_privileges(env, sequence):
    # (USAGE, UPDATE) for each runtime login.
    query = (
        "SELECT string_agg(has_sequence_privilege(login, '{0}', 'USAGE')::text"
        " || '/' || has_sequence_privilege(login, '{0}', 'UPDATE')::text, ' ' ORDER BY login)"
        " FROM unnest(ARRAY['grimoire_api', 'grimoire_worker']) login"
    ).format(sequence)
    return psql(env, "grimoire", "-c", query).stdout.split()


def test_runtime_logins_draw_but_cannot_set_sequence_values(restored):
    env = restored[0]
    sequences = {
        # A GENERATED ALWAYS identity, and a bigserial default that calls
        # nextval with the login's privileges.
        "grimoire.account": "grimoire.account_id_seq",
        "grimoire.campaign": "grimoire.campaign_id_seq",
    }
    for login in ("grimoire_api", "grimoire_worker"):
        psql(
            env,
            "grimoire",
            "-c",
            f"INSERT INTO grimoire.account (subject) VALUES ('{login}-sequence')",
            "-c",
            f"INSERT INTO grimoire.campaign (owner_id, name) VALUES (1, '{login}')",
            user=login,
        )
        for table, sequence in sequences.items():
            assert (
                psql(
                    env,
                    "grimoire",
                    "-c",
                    f"SELECT pg_get_serial_sequence('{table}', 'id')",
                ).stdout.strip()
                == sequence
            )
            rewind = psql(
                env,
                "grimoire",
                "-c",
                f"SELECT setval('{sequence}', 1)",
                user=login,
                check=False,
            )
            assert rewind.returncode != 0
            name = sequence.split(".")[1]
            assert f"permission denied for sequence {name}" in rewind.stderr
    for sequence in sequences.values():
        assert sequence_privileges(env, sequence) == ["true/false", "true/false"]


def test_grants_revoke_a_sequence_update_grant(restored):
    env = restored[0]
    # The grants that the first grants.sql made, on live and on later sequences.
    psql(
        env,
        "grimoire",
        "-c",
        "GRANT UPDATE ON ALL SEQUENCES IN SCHEMA grimoire TO grimoire_api, grimoire_worker",
        "-c",
        "ALTER DEFAULT PRIVILEGES FOR ROLE grimoire_restore"
        " GRANT UPDATE ON SEQUENCES TO grimoire_api, grimoire_worker",
        user="grimoire_restore",
    )
    assert sequence_privileges(env, "grimoire.campaign_id_seq") == [
        "true/true",
        "true/true",
    ]
    grants(env, "grimoire")
    assert sequence_privileges(env, "grimoire.campaign_id_seq") == [
        "true/false",
        "true/false",
    ]
    # A sequence that the restore owner makes later gets the default privileges.
    psql(
        env, "grimoire", "-c", "CREATE SEQUENCE grimoire.later", user="grimoire_restore"
    )
    try:
        assert sequence_privileges(env, "grimoire.later") == [
            "true/false",
            "true/false",
        ]
    finally:
        psql(
            env,
            "grimoire",
            "-c",
            "DROP SEQUENCE grimoire.later",
            user="grimoire_restore",
        )


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


def test_restore_accepts_key_text_in_an_index_predicate(restored):
    env, tmp, metadata, _ = restored
    psql(env, "postgres", "-c", "CREATE DATABASE predicate_source")
    psql(env, "predicate_source", "-c", PREDICATE_SOURCE)
    dump(env, "predicate_source", tmp / "predicate.dump")
    # The dump's post-data has a line that a text scan reads as a NOT VALID key.
    post_data = subprocess.run(
        ["pg_restore", "--section=post-data", "--file=-", tmp / "predicate.dump"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "\n FOREIGN KEY x NOT VALID;\n" in post_data
    create_target(env, "predicate")
    result = restore(env, tmp / "predicate.dump", metadata, "predicate")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 valid foreign keys and 0 valid pgvector indexes" in result.stdout
    assert inventory(env, "predicate") == inventory(env, "predicate_source")


def test_restore_refuses_other_pgvector_metadata(restored):
    env, tmp, _, _ = restored
    other = tmp / "other.metadata"
    other.write_text("0.0.1|public\n")
    result = restore(env, tmp / "grimoire.dump", other)
    assert result.returncode != 0
    assert "differ" in result.stdout + result.stderr


def test_restore_accepts_the_hub_dump_and_matches_its_inventory(restored):
    env, tmp, _, _ = restored
    pg_bin = Path(shutil.which("pg_dump", path=env["PATH"])).parent
    psql(env, "postgres", "-c", "CREATE DATABASE hub")
    psql(env, "hub", "-c", HUB_SOURCE)
    shim = tmp / "shim"
    shim.mkdir()
    (shim / "pg_dump").write_text(PG_DUMP_SHIM)
    (shim / "pg_dump").chmod(0o755)
    work = tmp / "hub-work"
    work.mkdir()
    result = subprocess.run(
        ["bash", runfile("HUB_DUMP_SH")],
        env=dict(
            env,
            PATH=f"{shim}:{env['PATH']}",
            REAL_PG_DUMP=str(pg_bin / "pg_dump"),
            DATABASE_URL=f"postgresql://test@{env['PGHOST']}:{env['PGPORT']}/hub",
            GRIMOIRE_SCHEMAS="grimoire",
            WORK=str(work),
            INVENTORY_SQL=str(runfile("HUB_INVENTORY_SQL")),
        ),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "dumped schemas grimoire: " in result.stdout
    source = (work / "source.inventory").read_text()
    assert "grimoire.campaign_member" in source
    assert "knowledge." not in source
    # The row written after the dump is live, but the inventory reads the
    # dump's snapshot.
    live = psql(env, "hub", "-v", "schemas=grimoire", "-f", runfile("INVENTORY_SQL"))
    assert live.stdout != source
    # The restore pod compares the target with source.inventory.
    compare = {
        "SOURCE_INVENTORY": str(work / "source.inventory"),
        "INVENTORY_SQL": str(runfile("INVENTORY_SQL")),
    }
    create_target(env, "from_hub")
    result = restore(
        env, work / "grimoire.dump", work / "vector.metadata", "from_hub", **compare
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "5 valid foreign keys and 2 valid pgvector indexes" in result.stdout
    assert "the restored database matches the source inventory" in result.stdout
    assert inventory(env, "from_hub") == source

    # A source inventory that differs fails the run and names the lines.
    assert "grimoire.account|3|" in source
    tampered = tmp / "tampered.inventory"
    tampered.write_text(source.replace("grimoire.account|3|", "grimoire.account|4|"))
    create_target(env, "from_hub_tampered")
    result = restore(
        env,
        work / "grimoire.dump",
        work / "vector.metadata",
        "from_hub_tampered",
        **dict(compare, SOURCE_INVENTORY=str(tampered)),
    )
    assert result.returncode != 0
    assert "differs from the source inventory" in result.stderr
    assert "grimoire.account|4|" in result.stderr


def test_hub_dump_refuses_a_schema_that_does_not_exist(restored):
    env, tmp, _, _ = restored
    work = tmp / "missing-work"
    work.mkdir()
    result = subprocess.run(
        ["bash", runfile("HUB_DUMP_SH")],
        env=dict(
            env,
            DATABASE_URL=f"postgresql://test@{env['PGHOST']}:{env['PGPORT']}/source",
            GRIMOIRE_SCHEMAS="grimoire,absent",
            WORK=str(work),
            INVENTORY_SQL=str(runfile("HUB_INVENTORY_SQL")),
        ),
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode != 0
    assert "no matching schemas were found" in result.stderr
    assert not (work / "source.inventory").exists()
