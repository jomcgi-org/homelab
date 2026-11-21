import subprocess
import sys

import pytest

from projects.factory.mandoline.execution import capture, verify_checkout


def test_timeout(tmp_path):
    command = [sys.executable, "-c", "import time; time.sleep(10)"]
    assert capture(command, tmp_path, 0.1, 32) == ("timeout", b"")


def test_truncation(tmp_path):
    command = [sys.executable, "-c", "print('x' * 4000)"]
    assert capture(command, tmp_path, 5, 32) == ("truncated", b"x" * 32)


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_hidden_edit(tmp_path, flag):
    def git(*args):
        return subprocess.check_output(["git", "-C", tmp_path, *args])

    git("init", "-q")
    source = tmp_path / "tracked"
    source.write_text("original")
    git("add", "tracked")
    git(
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.org",
        "commit",
        "-qm",
        "initial",
    )
    sha = git("rev-parse", "HEAD").decode().strip()
    verify_checkout(tmp_path, sha)
    git("update-index", flag, "tracked")
    source.write_text("modified")
    assert git("status", "--porcelain") == b""
    with pytest.raises(ValueError, match=sha):
        verify_checkout(tmp_path, sha)
