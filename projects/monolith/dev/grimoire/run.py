"""Start a disposable local table. Stop with Ctrl-C."""

import argparse
import os
import pwd
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[4]
WEB = ROOT / "projects/monolith/web/grimoire"
PORT = 8177
# The one browser origin: the web app, the invitation landing and the API.
# Chromium resolves *.localhost to loopback and treats it as trustworthy, so
# the landing's Secure cookie is stored here as it is over https in production.
ORIGIN = f"http://friends.localhost:{PORT}"
LOOPBACK = f"http://127.0.0.1:{PORT}"


def build_web(pnpm, directory):
    """Build the React bundle for /grimoire, exactly as the image build does."""
    subprocess.run(
        [pnpm, "exec", "vite", "build", "--outDir", str(directory), "--emptyOutDir"],
        cwd=WEB,
        env=os.environ | {"GRIMOIRE_BASE": "/grimoire/"},
        check=True,
        timeout=300,
        stdout=subprocess.DEVNULL,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rehearse", action="store_true", help="Run the browser rehearsal, then stop"
    )
    parser.add_argument(
        "--join-links",
        action="store_true",
        help="Rehearse registered-player links instead of the session table",
    )
    parser.add_argument("--output", default="/tmp/grimoire-rehearsal")
    args = parser.parse_args()
    pg_bin = Path(os.environ.get("GRIMOIRE_PG_BIN", "/usr/lib/postgresql/16/bin"))
    if not (pg_bin / "initdb").exists():
        raise SystemExit("Install PostgreSQL 16 with pgvector first.")
    # A caller that already built the bundle (ci.sh) names it; otherwise each
    # launch builds the current checkout into its own disposable directory.
    prebuilt = os.environ.get("GRIMOIRE_WEB_DIR")
    pnpm = shutil.which("pnpm")
    if prebuilt:
        if not (Path(prebuilt) / "index.html").is_file():
            raise SystemExit(f"GRIMOIRE_WEB_DIR has no index.html: {prebuilt}")
    elif not pnpm:
        raise SystemExit("Install pnpm first, or set GRIMOIRE_WEB_DIR to a build.")
    for port in (PORT, 55477):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError as error:
                raise SystemExit(
                    f"Port {port} is occupied; stop that local table first."
                ) from error

    def terminated(signum, _frame):
        # A plain SIGTERM would skip the cleanup below and orphan the database.
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, terminated)
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
            web_dir = Path(prebuilt) if prebuilt else state / "web"
            if not prebuilt:
                build_web(pnpm, web_dir)
            env = os.environ | {
                "PYTHONPATH": str(ROOT / "projects/monolith"),
                "DATABASE_URL": "postgresql://grimoire_local@127.0.0.1:55477/postgres",
                "GRIMOIRE_LOCAL_PLAYGROUND": "true",
                "GRIMOIRE_AUTH_ISSUER": f"{LOOPBACK}/__local/",
                "GRIMOIRE_AUTH_JWKS_URL": f"{LOOPBACK}/__local/jwks",
                "GRIMOIRE_AUTH_AUDIENCE": "grimoire-local",
                # grimoire/web.py serves this bundle at the base it was built for.
                "GRIMOIRE_WEB_DIR": str(web_dir),
                # The session cookie is a credential only for pages on this
                # origin, and a write must name it in its Origin header.
                "GRIMOIRE_BROWSER_ORIGIN": ORIGIN,
                # The playground's deterministic embedder; never a live model.
                "EMBEDDING_URL": f"{LOOPBACK}/__local",
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
                        str(PORT),
                    ],
                    cwd=ROOT,
                    env=env,
                )
            )
            # Ready means the identity keys and the app shell both answer, and
            # every script and stylesheet the shell names is served. A bundle
            # built for another base path fails here, before any browser runs.
            shell = ""
            for address in (f"{LOOPBACK}/__local/jwks", f"{LOOPBACK}/grimoire"):
                for _ in range(300):
                    if children[-1].poll() is not None:
                        raise RuntimeError("Backend exited before readiness")
                    try:
                        with urlopen(address, timeout=1) as response:
                            if response.status == 200:
                                shell = response.read().decode()
                                break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError(f"Server did not become ready: {address}")
            assets = re.findall(r'(?:src|href)="(/grimoire/assets/[^"]+)"', shell)
            if not assets:
                raise RuntimeError("The app shell names no /grimoire/assets file")
            for asset in assets:
                with urlopen(f"{LOOPBACK}{asset}", timeout=5) as response:
                    if response.status != 200 or "html" in response.headers.get(
                        "content-type", ""
                    ):
                        raise RuntimeError(f"App asset is not served: {asset}")
            print(f"App shell ready with {len(assets)} assets", flush=True)
            print(f"\nLocal table: {ORIGIN}/__local\n", flush=True)
            if args.rehearse:
                subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).with_name("rehearse.py")),
                        "--output",
                        args.output,
                        *(["--join-links"] if args.join_links else []),
                    ],
                    check=True,
                    timeout=900,
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
