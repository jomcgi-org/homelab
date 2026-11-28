import os
from pathlib import Path
import subprocess
import sys


def run_scan():
    root = Path(os.environ.get("TEST_SRCDIR", "")) / os.environ.get(
        "TEST_WORKSPACE", ""
    )
    if not os.environ.get("TEST_SRCDIR"):
        root = Path(__file__).resolve().parents[3]
    return subprocess.run(
        [sys.executable, root / "bazel/tools/ci/pr_content_scan.py", "HEAD^", "HEAD"],
        env={
            **os.environ,
            "PYTHONPATH": str(root / "projects/jomcgi-site/posts"),
        },
        capture_output=True,
        text=True,
    )


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
    result = run_scan()
    assert result.returncode == 1
    output = result.stdout + result.stderr
    assert "'example': GitHub token" in output
    assert "'new.conf': .internal hostname" in output
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    assert f"'commit {commit}': .internal hostname" in output
    assert f"'commit {commit}': GitHub token" in output
    assert "'new.conf': AWS access key" in output
    assert output.count("AWS access key") == 1
    assert all(value not in output for value in (token, untouched, marker))

    for addition, message, status in (
        ("export GH_" + 'TOKEN="${GHCR_' + 'TOKEN:-}"', "runtime reference", 0),
        ("GH_" + "TOKEN=$GHCR_TOKEN", "unquoted reference", 0),
        ("GH_" + 'TOKEN="${GHCR_' + 'TOKEN:-fixture}"', "literal default", 1),
        ("GH_" + "TOKEN=$GHCR_TOKEN OTHER_" + "SECRET=fixture", "mixed", 1),
        (
            "RENOVATE_" + 'TOKEN="$(cat /var/run/renovate-token/token)"',
            "mounted file",
            0,
        ),
        ("RENOVATE_" + 'TOKEN="$(printf fixture)"', "literal command argument", 1),
        (token, "fixture", 1),
        ("ordinary text", marker, 1),
        ("clean addition", "clean commit", 0),
    ):
        with file.open("a") as stream:
            stream.write(addition + "\n")
        git("add", ".")
        git("commit", "-m", message)
        assert run_scan().returncode == status
