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
    modified = tmp_path / "modified.conf"
    modified.write_text(untouched + "\n", encoding="utf-16")
    git("add", ".")
    git("commit", "-m", untouched)
    token = "ghp_" + "a" * 36
    marker = "service" + ".internal"
    file.write_text(untouched + "\n" + token + "\n")
    modified.write_text(untouched + "\nsafe addition\n", encoding="utf-16")
    (tmp_path / "new.conf").write_text(untouched + "\n" + marker, encoding="utf-16")
    git("add", ".")
    git("commit", "-m", marker, "-m", token)
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
    assert "'new.conf': .internal hostname" in output
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    assert f"'commit {commit}': .internal hostname" in output
    assert f"'commit {commit}': GitHub token" in output
    assert "'new.conf': AWS access key" in output
    assert output.count("AWS access key") == 1
    assert all(value not in output for value in (token, untouched, marker))
