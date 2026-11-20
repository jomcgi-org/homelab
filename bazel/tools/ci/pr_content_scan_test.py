import subprocess

from pr_content_scan import scan


def test_pr_scope_and_redaction(tmp_path, monkeypatch, capsys):
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
    scan("HEAD^", "HEAD")
    output = capsys.readouterr().out
    assert "'example': GitHub token" in output
    assert "'commit " in output and ".internal hostname" in output
    assert "AWS access key" not in output
    assert all(value not in output for value in (token, untouched, marker))
