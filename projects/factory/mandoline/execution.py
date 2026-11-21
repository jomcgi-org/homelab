"""Process limits for the private Mandoline runner."""

import os
import selectors
import signal
import subprocess
import time


def capture(argv, root, timeout, output_limit):
    """Capture combined diagnostics and output under a shared byte/time budget."""
    deadline = time.monotonic() + timeout
    with subprocess.Popen(
        argv,
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    ) as process:
        output = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        return "timeout", bytes(output)
                    capacity = output_limit - len(output)
                    chunk = os.read(process.stdout.fileno(), min(65536, capacity + 1))
                    if not chunk:
                        break
                    output.extend(chunk[:capacity])
                    if len(chunk) > capacity:
                        return "truncated", bytes(output)
            try:
                code = process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                return "timeout", bytes(output)
            return ("exited" if code == 0 else "error"), bytes(output)
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def verify_checkout(root, expected):
    """Require the supplied full commit and a clean sandbox checkout."""

    def git(*args):
        return subprocess.check_output(["git", "-C", root, *args])

    if git("rev-parse", "--show-prefix").strip():
        raise ValueError("sandbox root is inside a larger checkout")
    actual = git("rev-parse", "--verify", "HEAD").decode().strip()
    if actual != expected:
        raise ValueError(f"requested {expected}, checkout HEAD is {actual}")
    if git(
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
    ):
        raise ValueError(f"dirty checkout at {expected}")
