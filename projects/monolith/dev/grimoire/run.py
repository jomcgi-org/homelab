"""Start a disposable local table. Stop with Ctrl-C."""

import argparse
import os
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[4]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rehearse", action="store_true", help="Run the browser rehearsal, then stop"
    )
    parser.add_argument(
        "--join-links",
        action="store_true",
        help="Enable registered-player links in this disposable fixture only",
    )
    parser.add_argument("--output", default="/tmp/grimoire-rehearsal")
    args = parser.parse_args()
    pg_bin = Path(os.environ.get("GRIMOIRE_PG_BIN", "/usr/lib/postgresql/16/bin"))
    node = shutil.which("node")
    if not node or not (pg_bin / "initdb").exists():
        raise SystemExit("Install Node and PostgreSQL 16 with pgvector first.")
    for port in (4177, 8177, 55477):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError as error:
                raise SystemExit(
                    f"Port {port} is occupied; stop that local table first."
                ) from error
    children = []
    with tempfile.TemporaryDirectory(prefix="grimoire-local-") as directory:
        state = Path(directory)
        pg_process_identity = {}
        if os.geteuid() == 0:
            # CI can run as root; PostgreSQL must own only its disposable tree.
            account = pwd.getpwnam("postgres")
            os.chown(state, account.pw_uid, account.pw_gid)
            pg_process_identity = {
                "user": account.pw_uid,
                "group": account.pw_gid,
                "extra_groups": [],
            }
        subprocess.run(
            [
                str(pg_bin / "initdb"),
                "-D",
                str(state / "pg"),
                "-A",
                "trust",
                "-U",
                "grimoire_local",
                "--no-instructions",
            ],
            check=True,
            timeout=60,
            stdout=subprocess.DEVNULL,
            **pg_process_identity,
        )
        try:
            children.append(
                subprocess.Popen(
                    [
                        str(pg_bin / "postgres"),
                        "-D",
                        str(state / "pg"),
                        "-h",
                        "127.0.0.1",
                        "-p",
                        "55477",
                        "-k",
                        directory,
                    ],
                    stdout=(state / "postgres.log").open("w"),
                    stderr=subprocess.STDOUT,
                    **pg_process_identity,
                )
            )
            for _ in range(100):
                if children[0].poll() is not None:
                    raise RuntimeError((state / "postgres.log").read_text())
                ready = subprocess.run(
                    [str(pg_bin / "pg_isready"), "-h", "127.0.0.1", "-p", "55477"],
                    capture_output=True,
                    timeout=2,
                )
                if ready.returncode == 0:
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError((state / "postgres.log").read_text())
            env = os.environ | {
                "PYTHONPATH": str(ROOT / "projects/monolith"),
                "DATABASE_URL": "postgresql://grimoire_local@127.0.0.1:55477/postgres",
                "GRIMOIRE_LOCAL_PLAYGROUND": "true",
                "GRIMOIRE_PLAY_ENABLED": "true",
                "GRIMOIRE_AUTH_ISSUER": "http://127.0.0.1:8177/__local/",
                "GRIMOIRE_AUTH_JWKS_URL": "http://127.0.0.1:8177/__local/jwks",
                "GRIMOIRE_AUTH_AUDIENCE": "grimoire-local",
                "API_BASE": "http://127.0.0.1:8177",
                "GRIMOIRE_LOCAL_FRONTEND": "http://friends.localhost:4177",
                "VITE_GRIMOIRE_LOCAL": "true",
                "GRIMOIRE_INVITATION_LINKS_ENABLED": str(args.join_links).lower(),
                # Never inherit a live enrollment provider into this fixture.
                "GRIMOIRE_INVITATION_ENROLLMENT_ENABLED": "false",
                "GRIMOIRE_INVITATION_API_TOKEN": "",
                "GRIMOIRE_INVITATION_FLOW_ID": "",
            }
            children.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "dev.grimoire.app:build_app",
                        "--factory",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "8177",
                    ],
                    cwd=ROOT,
                    env=env,
                )
            )
            children.append(
                subprocess.Popen(
                    [
                        node,
                        "test/grimoire-dev-server.mjs",
                        "--cache-dir",
                        str(state / "vite-cache"),
                        "--ready-file",
                        str(state / "frontend-ready.json"),
                    ],
                    cwd=ROOT / "projects/monolith/frontend",
                    env=env,
                )
            )
            # Every table owns a cold dependency cache. Wait for the running
            # server's committed batch, not merely its Vite runtime endpoint.
            for _ in range(100):
                if children[-1].poll() is not None:
                    raise RuntimeError("Frontend exited before dependency readiness")
                if (state / "frontend-ready.json").exists():
                    break
                time.sleep(0.1)
            else:
                raise RuntimeError("Frontend dependencies did not become ready")
            print("\nLocal table: http://friends.localhost:8177/__local\n", flush=True)
            if args.rehearse:
                for address in [
                    "http://127.0.0.1:8177/__local/jwks",
                    "http://127.0.0.1:4177/@vite/client",
                ]:
                    for _ in range(100):
                        try:
                            with urlopen(address, timeout=1) as response:
                                if response.status == 200:
                                    break
                        except OSError:
                            time.sleep(0.1)
                    else:
                        raise RuntimeError(f"Server did not become ready: {address}")
                subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).with_name("rehearse.py")),
                        "--output",
                        args.output,
                        *(["--join-links"] if args.join_links else []),
                    ],
                    check=True,
                    timeout=180,
                )
                return
            while all(child.poll() is None for child in children):
                time.sleep(0.5)
            raise RuntimeError("A playground process exited; see output above")
        except KeyboardInterrupt:
            pass
        finally:
            for child in reversed(children):
                if child.poll() is None:
                    child.terminate()
            for child in children:
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)


if __name__ == "__main__":
    main()
