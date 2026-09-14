#!/usr/bin/env python3
"""Rebuild/run the Apple Firecracker probe and local development server."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid


HERE = Path(__file__).resolve().parent
REPO = HERE.parents[3]


def run(*args, **kwargs):
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def artifacts():
    # Use the deployment's existing Firecracker pin, including its checksum.
    module = (REPO / "MODULE.bazel").read_text()
    block = re.search(
        r"kata_firecracker_archive\(\s*name = \"kata_firecracker\",(.*?)\n\)",
        module,
        re.S,
    )
    if block is None:
        raise RuntimeError("cannot find the Firecracker artifact pins in MODULE.bazel")
    fields = dict(re.findall(r'(\w+) = "([^"]+)"', block.group(1)))
    return {
        "kernel": json.loads((HERE / "kernel.json").read_text()),
        "firecracker": {
            "url": fields["arm64_firecracker_url"],
            "sha256": fields["arm64_firecracker_sha256"],
            "version": fields["firecracker_version"],
        },
    }


def serve(args, guest_tmp):
    instance = json.loads(
        run(
            "limactl", "list", "--json", args.instance, capture_output=True, text=True
        ).stdout
    )
    session = uuid.uuid4().hex
    url = f"http://127.0.0.1:{args.port}"
    # A dedicated SSH connection owns both the loopback tunnel and remote TTY.
    # Closing it hangs up the runner, which drains the Go server and its VMs.
    process = subprocess.Popen(
        [
            "ssh",
            "-F",
            instance["sshConfigFile"],
            "-S",
            "none",
            "-n",
            "-tt",
            "-o",
            "ControlMaster=no",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=3",
            "-L",
            f"127.0.0.1:{args.port}:127.0.0.1:8080",
            instance["hostname"],
            "sudo",
            "python3",
            f"{guest_tmp}/run_linux.py",
            "--serve",
            "--session",
            session,
        ],
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 180
        while True:
            if process.poll() is not None:
                raise RuntimeError(
                    f"server exited during startup ({process.returncode})"
                )
            try:
                with urllib.request.urlopen(url + "/status", timeout=2) as response:
                    code, status = response.status, json.load(response)
            except (OSError, ValueError):
                code, status = 0, {}
            if code == 200:
                if status.get("session") != session:
                    raise RuntimeError(f"another server is already using {url}")
                if status["ready"] == status["capacity"]:
                    break
            if time.monotonic() > deadline:
                raise RuntimeError("server did not prime its pool within three minutes")
            time.sleep(0.2)
        print(
            f"\nServer ready: {url} ({status['capacity']} primed Firecracker VMs)",
            flush=True,
        )
        print(f"  curl {url}/status", flush=True)
        print(
            f"  curl {url}/scan -H 'Content-Type: application/json' -d '{{}}'",
            flush=True,
        )
        print(
            "\nServer stays running. Use another terminal to send requests; Ctrl-C drains and stops it.",
            flush=True,
        )
        if process.wait() != 0:
            raise RuntimeError("development server failed; see the Linux server.log")
    finally:
        if process.poll() is None:
            # Keep the tunnel open while accepted requests drain. The session
            # nonce ensures a failed second start cannot stop another runner.
            try:
                stopped = subprocess.run(
                    [
                        "limactl",
                        "shell",
                        "--workdir=/",
                        args.instance,
                        "sudo",
                        "python3",
                        f"{guest_tmp}/run_linux.py",
                        "--stop",
                        "--session",
                        session,
                    ],
                    timeout=10,
                )
                stop_failed = stopped.returncode != 0
            except (OSError, subprocess.TimeoutExpired):
                stop_failed = True
            if stop_failed:
                process.terminate()
            try:
                process.wait(timeout=40)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["up", "run", "serve", "status", "stop"])
    parser.add_argument("--instance", default="embervm-dev")
    parser.add_argument("--port", type=int, default=8080, help="Mac loopback HTTP port")
    parser.add_argument(
        "--binaries",
        type=Path,
        help="directory containing prebuilt Linux ARM64 probe and guest binaries",
    )
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be 1024..65535")
    if args.binaries is not None:
        args.binaries = args.binaries.resolve()
        if not all((args.binaries / name).is_file() for name in ("probe", "guest")):
            parser.error("--binaries must contain both probe and guest files")
    if shutil.which("limactl") is None:
        parser.error("install Lima first: brew install lima")
    if not re.fullmatch(r"[a-z][a-z0-9-]*", args.instance):
        parser.error("instance name must use lowercase letters, digits and hyphens")
    if args.command == "status":
        run("limactl", "list", args.instance)
        return
    if args.command == "stop":
        run("limactl", "stop", args.instance)
        return
    instances = run("limactl", "list", "--json", capture_output=True, text=True)
    existing = next(
        (
            json.loads(line)
            for line in instances.stdout.splitlines()
            if json.loads(line)["name"] == args.instance
        ),
        None,
    )
    if existing is not None:
        if (
            existing["vmType"] != "vz"
            or existing["arch"] != "aarch64"
            or existing["cpus"] != 4
            or existing["memory"] != 10 * 1024**3
            or not existing["config"].get("nestedVirtualization")
        ):
            parser.error(
                "existing instance does not match the 10 GiB / 4 CPU nested ARM host; choose a new --instance"
            )
        run("limactl", "start", "--tty=false", "--timeout=10m", args.instance)
    else:
        run(
            "limactl",
            "start",
            "--tty=false",
            "--timeout=10m",
            f"--name={args.instance}",
            HERE / "lima.yaml",
        )
    if args.command == "up":
        return
    if args.binaries is None and shutil.which("go") is None:
        parser.error(
            "install the Go version required by go.mod before running the probe"
        )
    env = dict(os.environ, GOOS="linux", GOARCH="arm64", CGO_ENABLED="0")
    with tempfile.TemporaryDirectory(prefix="embervm-dev-") as local:
        local = Path(local)
        for name, package in [
            ("probe", "./projects/embervm/noded/cmd/dev-smoke"),
            ("guest", "./projects/embervm/noded/cmd/dev-smoke/guest"),
        ]:
            if args.binaries is not None:
                shutil.copyfile(args.binaries / name, local / name)
                continue
            run(
                "go",
                "build",
                "-mod=readonly",
                "-trimpath",
                "-o",
                local / name,
                package,
                cwd=REPO,
                env=env,
            )
        (local / "artifacts.json").write_text(json.dumps(artifacts()))
        shutil.copyfile(HERE / "run_linux.py", local / "run_linux.py")
        guest_tmp = run(
            "limactl",
            "shell",
            "--workdir=/",
            args.instance,
            "mktemp",
            "-d",
            "/tmp/embervm-dev.XXXXXX",
            capture_output=True,
            text=True,
        ).stdout.strip()
        if not re.fullmatch(r"/tmp/embervm-dev\.[A-Za-z0-9]+", guest_tmp):
            raise RuntimeError(f"unexpected guest temporary directory: {guest_tmp!r}")
        try:
            run(
                "limactl",
                "copy",
                *(
                    local / name
                    for name in ["probe", "guest", "artifacts.json", "run_linux.py"]
                ),
                f"{args.instance}:{guest_tmp}/",
            )
            if args.command == "serve":
                serve(args, guest_tmp)
            else:
                run(
                    "limactl",
                    "shell",
                    "--workdir=/",
                    args.instance,
                    "sudo",
                    "python3",
                    f"{guest_tmp}/run_linux.py",
                )
        finally:
            run(
                "limactl",
                "shell",
                "--workdir=/",
                args.instance,
                "rm",
                "-rf",
                "--",
                guest_tmp,
            )


if __name__ == "__main__":

    def interrupt(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGHUP, interrupt)
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
    except (RuntimeError, OSError, subprocess.CalledProcessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
