"""Start a disposable local table. Stop with Ctrl-C."""

import argparse
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[4]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rehearse", action="store_true", help="Run the browser rehearsal, then stop"
    )
    parser.add_argument("--output", default="/tmp/grimoire-rehearsal")
    args = parser.parse_args()
    pg_bin = Path(os.environ.get("GRIMOIRE_PG_BIN", "/usr/lib/postgresql/16/bin"))
    pnpm = shutil.which("pnpm")
    if not pnpm or not (pg_bin / "initdb").exists():
        raise SystemExit("Install pnpm and PostgreSQL 16 with pgvector first.")
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
                        pnpm,
                        "exec",
                        "vite",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        "4177",
                        "--strictPort",
                    ],
                    cwd=ROOT / "projects/monolith/frontend",
                    env=env,
                )
            )
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
