import os
from pathlib import Path
import subprocess
import sys


def test_pr_scope_and_redaction(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def git(*args):
        subprocess.run(["git", *args], check=True, capture_output=True)

    git("init")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "test@example.org")
    file = tmp_path / "example"
    untouched = "AKIA" + "X" * 16
    file.write_text(untouched + "\n")
    git("add", ".")
    git("commit", "-m", untouched)
    token = "ghp_" + "a" * 36
    marker = "service" + ".internal"
    file.write_text(untouched + "\n" + token + "\n")
    git("commit", "-am", marker)
    root = Path(os.environ.get("TEST_SRCDIR", "")) / os.environ.get(
        "TEST_WORKSPACE", ""
    )
    if not os.environ.get("TEST_SRCDIR"):
        root = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [sys.executable, root / "bazel/tools/ci/pr_content_scan.py", "HEAD^", "HEAD"],
        env={
            **os.environ,
            "PYTHONPATH": str(root / "projects/monolith/knowledge/tools"),
        },
        check=True,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    assert "'example': GitHub token" in output
    assert "'commit " in output and ".internal hostname" in output
    assert "AWS access key" not in output
    assert all(value not in output for value in (token, untouched, marker))
