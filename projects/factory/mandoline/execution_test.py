import sys

from projects.factory.mandoline.execution import capture


def test_timeout(tmp_path):
    command = [sys.executable, "-c", "import time; time.sleep(10)"]
    assert capture(command, tmp_path, 0.1, 32) == ("timeout", b"")


def test_truncation(tmp_path):
    command = [sys.executable, "-c", "print('x' * 4000)"]
    assert capture(command, tmp_path, 5, 32) == ("truncated", b"x" * 32)
