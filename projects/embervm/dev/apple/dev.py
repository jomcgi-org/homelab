#!/usr/bin/env python3
"""Start the Apple development host and rebuild/run the real Firecracker probe."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["up", "run", "status", "stop"])
    parser.add_argument("--instance", default="embervm-dev")
    args = parser.parse_args()
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
    if shutil.which("go") is None:
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
    main()
