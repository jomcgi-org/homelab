"""Prepare checksum-verified artifacts and execute the probe inside Lima."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tarfile
import tempfile
import urllib.request


ROOT = Path("/var/lib/embervm-dev")


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(spec):
    path = ROOT / "artifacts" / spec["sha256"]
    if path.exists() and digest(path) == spec["sha256"]:
        return path
    temporary = path.with_suffix(".partial")
    try:
        with (
            urllib.request.urlopen(spec["url"], timeout=60) as source,
            temporary.open("wb") as target,
        ):
            shutil.copyfileobj(source, target)
        if digest(temporary) != spec["sha256"]:
            raise RuntimeError(f"checksum mismatch: {spec['url']}")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--session", default="")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("run inside the development VM with sudo")
    session_file = ROOT / "server-session.json"
    if args.stop:
        if not args.session:
            parser.error("--stop requires the owning runner's --session")
        if session_file.exists():
            owner = json.loads(session_file.read_text())
            if owner["session"] == args.session:
                try:
                    if os.readlink(f"/proc/{owner['pid']}/exe") == str(ROOT / "probe"):
                        os.kill(owner["pid"], signal.SIGTERM)
                except FileNotFoundError:
                    pass
        return
    source = Path(__file__).resolve().parent
    ROOT.mkdir(mode=0o750, parents=True, exist_ok=True)
    # Serialize preparation as well as execution, so a second rebuild cannot
    # replace the host executable while a running probe re-execs it.
    with (ROOT / "prepare.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        kvm = os.open("/dev/kvm", os.O_RDWR)
        try:
            if fcntl.ioctl(kvm, 0xAE00) != 12:
                raise RuntimeError("unsupported KVM API version")
            vm = fcntl.ioctl(kvm, 0xAE01, 0)
            os.close(vm)
        finally:
            os.close(kvm)
        (ROOT / "artifacts").mkdir(exist_ok=True)
        spec = json.loads((source / "artifacts.json").read_text())
        kernel = download(spec["kernel"])
        archive = download(spec["firecracker"])
        version = spec["firecracker"]["version"]
        fc = ROOT / "artifacts" / f"firecracker-{spec['firecracker']['sha256']}"
        with tarfile.open(archive, "r:gz") as tar:
            member = f"release-{version}-aarch64/firecracker-{version}-aarch64"
            with tar.extractfile(member) as binary, fc.open("wb") as target:
                shutil.copyfileobj(binary, target)
        fc.chmod(0o755)
        # Each guest binary gets an immutable rootfs path. Rebuilding only the
        # host leaves both the rootfs bytes/UUID and its warmed base reusable.
        rootfs = ROOT / "artifacts" / f"probe-{digest(source / 'guest')}.ext4"
        if not rootfs.exists():
            with tempfile.TemporaryDirectory(dir=ROOT) as tree:
                tree = Path(tree)
                for name in ["proc", "sys", "dev", "tmp"]:
                    (tree / name).mkdir()
                shutil.copyfile(source / "guest", tree / "init")
                (tree / "init").chmod(0o755)
                temporary = rootfs.with_suffix(".partial")
                with temporary.open("wb") as file:
                    file.truncate(32 * 1024 * 1024)
                subprocess.run(
                    ["mkfs.ext4", "-q", "-F", "-d", str(tree), str(temporary)],
                    check=True,
                )
                temporary.replace(rootfs)
        probe = ROOT / "probe"
        shutil.copyfile(source / "probe", probe)
        probe.chmod(0o755)
        log = ROOT / ("server.log" if args.serve else "last-run.log")
        extra = (
            ["-listen", "127.0.0.1:8080", "-session", args.session]
            if args.serve
            else []
        )
        with log.open("w") as output:
            process = subprocess.Popen(
                [
                    str(probe),
                    "-kernel",
                    str(kernel),
                    "-rootfs",
                    str(rootfs),
                    "-firecracker",
                    str(fc),
                    "-vms",
                    "4",
                    "-mem-mib",
                    "1536",
                    *extra,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            if args.serve:
                session_file.write_text(
                    json.dumps({"session": args.session, "pid": process.pid})
                )
                session_file.chmod(0o600)
            # Let the Go probe reap its VMs on terminal disconnect or Ctrl-C.
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                signal.signal(
                    sig, lambda _signum, _frame: process.send_signal(signal.SIGTERM)
                )
            for line in process.stdout:
                output.write(line)
                output.flush()
                if any(
                    label in line
                    for label in (
                        "reuse base ",
                        "cold boot + ready",
                        "wave ",
                        "PASS:",
                        "server:",
                    )
                ):
                    print(line, end="", flush=True)
            status = process.wait()
            if args.serve:
                session_file.unlink(missing_ok=True)
        lines = log.read_text().splitlines()
        if status:
            print("\n".join(lines[-80:]), flush=True)
            raise RuntimeError(
                f"probe failed (exit {status}); full guest-host log: {log}"
            )
        print(f"Full log inside the Linux host: {log}")


if __name__ == "__main__":
    main()
