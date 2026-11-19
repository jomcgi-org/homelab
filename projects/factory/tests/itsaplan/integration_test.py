"""Run the actual upstream API and chart adapter with a private PostgreSQL."""

import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import tarfile
import time

import pytest


def run(command, **kwargs):
    result = subprocess.run(
        [str(part) for part in command],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
        **kwargs,
    )
    if result.returncode:
        pytest.fail(result.stdout + result.stderr, pytrace=False)
    return result


def pg_paths():
    # Explicit paths also let a developer run this launcher with local tools.
    if os.environ.get("PG_BIN"):
        return Path(os.environ["PG_BIN"]), Path(os.environ["PG_SHARE"])
    srcdir = Path(os.environ["TEST_SRCDIR"])
    for name in ("+postgres+postgres_test", "postgres_test"):
        root = srcdir / name
        binary = root / "usr/lib/postgresql/16/bin"
        if (binary / "postgres").exists():
            return binary, root / "usr/share/postgresql/16"
    pytest.fail("@postgres_test runfiles are missing", pytrace=False)


def pg_user():
    if os.getuid() == 0:
        nobody = pwd.getpwnam("nobody")
        os.setgid(nobody.pw_gid)
        os.setuid(nobody.pw_uid)


def test_upstream_factory_contracts(tmp_path):
    bun = Path(os.environ["BUN_BIN"]).resolve()
    archive = Path(os.environ["ITSAPLAN_ARCHIVE"]).resolve()
    upstream = tmp_path / "upstream"
    with tarfile.open(archive) as fixture:
        fixture.extractall(upstream, filter="data")
    adapter = Path(__file__).resolve().parents[2] / "chart/files/itsaplan"
    for src, dest in {
        "client.ts": "packages/db/src/client.ts",
        "transaction.ts": "packages/db/src/factory-transaction.ts",
        "deferred.ts": "apps/api/src/factory-deferred.ts",
        "factory.ts": "apps/api/src/factory.ts",
    }.items():
        shutil.copyfile(adapter / src, upstream / dest)
    shutil.copyfile(
        Path(__file__).with_name("deferred.test.ts"),
        upstream / "apps/api/src/factory-deferred.test.ts",
    )
    shutil.copyfile(
        Path(__file__).with_name("token-usage.test.ts"),
        upstream / "apps/api/src/factory-token-usage.test.ts",
    )
    connector = Path(__file__).resolve().parents[2] / "connector"

    pg_bin, pg_share = pg_paths()
    # Never write the shared runfiles. Some OCI layers omit these templates.
    share = tmp_path / "pg-share"
    shutil.copytree(pg_share, share)
    for name, content in {
        "postgresql.conf.sample": "listen_addresses = '127.0.0.1'\n",
        "pg_hba.conf.sample": "host all all 127.0.0.1/32 trust\n",
        "pg_ident.conf.sample": "# test identity map\n",
    }.items():
        if not (share / name).exists():
            (share / name).write_text(content)
    datadir = tmp_path / "pg-data"
    datadir.mkdir()
    if os.getuid() == 0:
        nobody = pwd.getpwnam("nobody")
        os.chown(datadir, nobody.pw_uid, nobody.pw_gid)
        # pytest and Bazel both use private temp parents; the PG child needs
        # traversal, while only its own data directory becomes writable.
        for parent in (tmp_path, *tmp_path.parents):
            parent.chmod(parent.stat().st_mode | 0o111)
    run(
        [pg_bin / "initdb", "-D", datadir, "-U", "test", "--no-locale", "-L", share],
        preexec_fn=pg_user,
    )
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    with (tmp_path / "postgres.log").open("w+") as log:
        postgres = subprocess.Popen(
            [
                str(pg_bin / "postgres"),
                "-D",
                str(datadir),
                "-h",
                "127.0.0.1",
                "-p",
                str(port),
                "-c",
                "unix_socket_directories=",
                "-c",
                "fsync=off",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            preexec_fn=pg_user,
        )
        try:
            deadline = time.monotonic() + 20
            while True:
                ready = subprocess.run(
                    [
                        str(pg_bin / "pg_isready"),
                        "-h",
                        "127.0.0.1",
                        "-p",
                        str(port),
                        "-U",
                        "test",
                    ],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                if ready.returncode == 0:
                    break
                if postgres.poll() is not None or time.monotonic() >= deadline:
                    log.seek(0)
                    pytest.fail(log.read(), pytrace=False)
                time.sleep(0.1)
            # Always use this private database and synthetic auth credentials.
            env = {
                **os.environ,
                "DECISION_PROBE": str(Path(os.environ["DECISION_PROBE"]).resolve()),
                "NODE_ENV": "test",
                "FACTORY_INTSEC_SYNC": "true",
                "SKIP_PRE_MIGRATION_BACKUP": "1",
                "DATABASE_URL": f"postgres://test@127.0.0.1:{port}/postgres",
                "APP_URL": "http://localhost:3001",
                "API_URL": "http://localhost:3000",
                "BETTER_AUTH_SECRET": "local-test-secret-that-is-not-a-credential",
                "APP_ENCRYPTION_KEY": "0" * 64,
                "BUN_RUNTIME_TRANSPILER_CACHE_PATH": "0",
                "PSQL_BIN": str(pg_bin / "psql"),
                "USAGE_REPORT": str(connector / "itsaplan/report-delivered-usage.sh"),
            }
            run(
                [
                    pg_bin / "psql",
                    "--dbname",
                    env["DATABASE_URL"],
                    "-X",
                    "--set=ON_ERROR_STOP=1",
                    "-c",
                    "CREATE DATABASE factory_test",
                    "-c",
                    "CREATE DATABASE connector_test",
                    "-c",
                    "CREATE ROLE factory_broker_accounting",
                    "-c",
                    "CREATE ROLE factory_storyboard",
                    "-c",
                    "CREATE ROLE factory_reaper",
                    "-c",
                    "CREATE ROLE factory_leaderboard",
                ],
                cwd=upstream,
                env=env,
            )
            run(
                [
                    pg_bin / "psql",
                    "-X",
                    env["DATABASE_URL"],
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-f",
                    os.environ.get(
                        "CLAIMS_TEST",
                        connector.parents[1]
                        / "platform-factory/kagent-postgres/resources/claims_test.sql",
                    ),
                ],
                env=env,
            )
            env["DATABASE_URL"] = f"postgres://test@127.0.0.1:{port}/factory_test"
            env["ITSAPLAN_DATABASE_URL"] = env["DATABASE_URL"]
            env["FACTORY_CONNECTOR_DATABASE_URL"] = (
                f"postgres://test@127.0.0.1:{port}/connector_test?sslmode=disable"
            )
            run([bun, "run", "packages/db/src/migrate.ts"], cwd=upstream, env=env)
            migration_args = [
                pg_bin / "psql",
                "--dbname",
                env["FACTORY_CONNECTOR_DATABASE_URL"],
                "-X",
                "-q",
                "--single-transaction",
                "--set=ON_ERROR_STOP=1",
            ]
            for migration in sorted((connector / "migrations").glob("*.sql")):
                if migration.name == "20261119110000_delivery_outcomes.sql":
                    fixtures = Path(__file__).parent
                    migration_args.extend(
                        [
                            "-c",
                            "SAVEPOINT before_outcomes",
                            "-f",
                            fixtures / "delivery-outcome-backfill.sql",
                            "-f",
                            migration,
                            "-f",
                            fixtures / "delivery-outcome-assertions.sql",
                            "-c",
                            "ROLLBACK TO SAVEPOINT before_outcomes",
                        ]
                    )
                migration_args.extend(["-f", migration])
            run(
                migration_args,
                env=env,
            )
            result = run(
                [
                    bun,
                    "test",
                    "src/factory-deferred.test.ts",
                    "src/factory-token-usage.test.ts",
                ],
                cwd=upstream / "apps/api",
                env=env,
            )
            print(result.stdout + result.stderr)
        finally:
            postgres.terminate()
            try:
                postgres.wait(timeout=10)
            except subprocess.TimeoutExpired:
                postgres.kill()
                postgres.wait(timeout=5)
