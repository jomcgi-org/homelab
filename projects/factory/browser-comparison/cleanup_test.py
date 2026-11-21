"""Exercise the Linux supervisor with detached descendants and racing procfs reads."""

import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import time

import pytest


@pytest.mark.parametrize("signum", [signal.SIGINT, signal.SIGALRM])
def test_interrupted_descendant(tmp_path, monkeypatch, signum):
    spec = importlib.util.spec_from_file_location(
        "compare", Path(__file__).with_name("compare.py")
    )
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    driver.adopt_descendants()
    # The launcher exits after starting a new session, like the CLI daemon.
    child = int(
        subprocess.check_output(
            ["sh", "-c", "setsid sleep 15 >/dev/null 2>&1 & echo $!"]
        )
    )
    read = Path.read_text
    missed = False

    def first_read_empty(path):
        nonlocal missed
        if not missed:
            missed = True
            return ""
        return read(path)

    monkeypatch.setattr(Path, "read_text", first_read_empty)
    previous = signal.signal(signum, driver.interrupt)
    try:
        with pytest.raises(driver.Interrupted):
            try:
                if signum == signal.SIGALRM:
                    signal.setitimer(signal.ITIMER_REAL, 0.02)
                    time.sleep(1)
                else:
                    signal.raise_signal(signum)
            finally:
                driver.cleanup()
        with pytest.raises(ProcessLookupError):
            os.kill(child, 0)
        with pytest.raises(ChildProcessError):
            os.waitpid(-1, os.WNOHANG)
    finally:
        signal.signal(signum, previous)
