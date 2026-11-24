from pathlib import Path

import pytest
import yaml

from check_invariants import InvariantLoader, eligible_tests, validate

ROW = dict(
    id="DEMO-1",
    statement="Something holds.",
    mechanism=["foo/bar"],
    state="live",
    test="//foo:test",
)


def test_states():
    live = validate(
        {
            "scope": {
                "invariants": [
                    ROW,
                    dict(
                        id="DEMO-2",
                        statement="Designed.",
                        mechanism=[],
                        state="designed",
                    ),
                    dict(
                        id="DEMO-3",
                        statement="Unproven.",
                        mechanism=["foo"],
                        state="unproven",
                    ),
                ]
            }
        }
    )
    assert live == {"//foo:test": ["scope: DEMO-1"]}


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "bad"},
        {"statement": " "},
        {"state": "unknown"},
        {"mechanism": "foo"},
        {"mechanism": ["/absolute"]},
        {"mechanism": ["../foo"]},
        {"mechanism": []},
        {"state": "designed"},
        {"test": None},
        {"test": "foo"},
        {"extra": None},
    ],
)
def test_bad_row(changes):
    with pytest.raises(ValueError):
        validate({"scope": {"invariants": [ROW | changes]}})


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        {"invariants": {}},
        {"invariants": [{}]},
        {"invariants": [{k: v for k, v in ROW.items() if k != "test"}]},
    ],
)
def test_bad_shape_or_missing_field(document):
    with pytest.raises(ValueError):
        validate({"scope": document})


def test_global_duplicate():
    with pytest.raises(ValueError, match="duplicate"):
        validate({"a": {"invariants": [ROW]}, "b": {"invariants": [ROW]}})


def test_issue_key_ids_do_not_collide():
    # HOME-758: two PRs for different issues add IDs without coordination.
    first = ROW | {"id": "HOME-123-1"}
    second = ROW | {"id": "HOME-124-1"}
    validate({"a": {"invariants": [first, second]}})
    with pytest.raises(ValueError, match="duplicate HOME-123-1"):
        validate({"a": {"invariants": [first]}, "b": {"invariants": [first]}})


@pytest.mark.parametrize(
    "rule, expected",
    [
        ("", False),
        ('<rule class="go_library" name="//foo:test"/>', False),
        (
            '<rule class="go_test" name="//foo:test"><list name="tags"><string value="manual"/></list></rule>',
            False,
        ),
        (
            '<rule class="py_test" name="//foo:test"><list name="tags"><string value="manual-review"/></list></rule>',
            True,
        ),
    ],
)
def test_live_test_eligibility(rule, expected):
    assert ("//foo:test" in eligible_tests("<query>" + rule + "</query>")) == expected


def test_duplicate_yaml_field():
    with pytest.raises(yaml.YAMLError, match="duplicate mapping key"):
        yaml.load("state: live\nstate: designed", Loader=InvariantLoader)


def test_local_lint_and_required_ci(tmp_path, monkeypatch):
    import subprocess

    from check_invariants import check

    monkeypatch.chdir(tmp_path)
    path = tmp_path / "INVARIANTS.yaml"
    path.write_text(yaml.safe_dump({"invariants": [ROW]}))

    def output(command):
        if command[0] == "git":
            return b"INVARIANTS.yaml\0"
        assert command == ["bazel", "query", "--output=xml", "set(//foo:test)"]
        return b'<query><rule name="//foo:test" class="py_test"><list name="tags"><string value="manual"/></list></rule></query>'

    monkeypatch.setattr(subprocess, "check_output", output)
    check(records_only=True)
    with pytest.raises(ValueError, match="must be a non-manual test"):
        check()
    path.write_text(yaml.safe_dump({"invariants": [ROW | {"state": "typo"}]}))
    with pytest.raises(ValueError, match="unknown state"):
        check(records_only=True)


def test_cached_bundle_without_yaml(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path

    import check_invariants

    # Simulate an interpreter that cannot find the yaml package.
    (tmp_path / "yaml.py").write_text(
        "raise ModuleNotFoundError('missing yaml', name='yaml')"
    )
    command = [sys.executable, str(Path(check_invariants.__file__).resolve())]
    env = dict(os.environ, PYTHONPATH=str(tmp_path))
    local = subprocess.run(
        [*command, "--records-only"], env=env, capture_output=True, text=True
    )
    assert local.returncode == 0
    assert "./bootstrap.sh" in local.stderr
    required = subprocess.run(command, env=env, capture_output=True, text=True)
    assert required.returncode != 0
    assert "ModuleNotFoundError" in required.stderr


@pytest.mark.parametrize(
    ("replacement", "error"),
    [
        ("", "dropped invariant marker"),
        ("unrelated", "dropped invariant marker"),
        ("rename", None),
        ("# invariant: UNKNOWN-8", "unknown invariant UNKNOWN-8"),
    ],
)
def test_marker_regressions(tmp_path, monkeypatch, replacement, error):
    import subprocess

    from check_invariants import check_markers

    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], check=True)
    docs = {Path("INVARIANTS.yaml"): {"invariants": [ROW]}}
    mechanism = Path(ROW["mechanism"][0])
    mechanism.parent.mkdir(parents=True, exist_ok=True)
    marker = "# invariant: " + ROW["id"]
    mechanism.write_text(marker)
    test = Path("test.py")
    test.write_text(marker)

    def commit():
        subprocess.run(["git", "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=test",
                "-c",
                "user.email=test@example.org",
                "commit",
                "-qm",
                "fixture",
            ],
            check=True,
        )

    commit()
    if replacement == "rename":
        test.rename("renamed.py")
    elif replacement == "unrelated":
        test.write_text("")
        Path("unrelated.py").write_text(marker)
    else:
        test.write_text(replacement)
    commit()
    if error is None:
        check_markers(docs, "HEAD^")
        return
    with pytest.raises(ValueError, match=error):
        check_markers(docs, "HEAD^")


def git(*args):
    import subprocess

    subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.org", *args],
        check=True,
        capture_output=True,
    )


def write_rows(path, *ids):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"invariants": [ROW | {"id": i} for i in ids]}))


def stale_pr(tmp_path, monkeypatch, pr_ids, main_ids):
    """Fork `pr` and `main` from one commit, then add rows on each side."""
    monkeypatch.chdir(tmp_path)
    git("init", "-q", "-b", "main")
    write_rows("INVARIANTS.yaml", "DEMO-1")
    git("add", ".")
    git("commit", "-qm", "base")
    git("checkout", "-qb", "pr")
    write_rows("pr/INVARIANTS.yaml", *pr_ids)
    git("add", ".")
    git("commit", "-qm", "pr")
    git("checkout", "-q", "main")
    write_rows("main/INVARIANTS.yaml", *main_ids)
    git("add", ".")
    git("commit", "-qm", "main")
    git("checkout", "-q", "pr")


def test_added_id_that_target_has_fails(tmp_path, monkeypatch):
    import subprocess

    from check_invariants import check

    stale_pr(tmp_path, monkeypatch, ["FACT-19"], ["FACT-19"])
    sha = subprocess.check_output(
        ["git", "rev-parse", "--short=12", "main"], text=True
    ).strip()
    # The PR's own tree has no duplicate, so the tree-only check passes.
    check(records_only=True)
    with pytest.raises(ValueError) as error:
        check(records_only=True, target="main")
    message = str(error.value)
    assert "pr/INVARIANTS.yaml: FACT-19 is new in this PR" in message
    assert "main/INVARIANTS.yaml on main (" + sha + ")" in message


def test_added_id_the_target_lacks_passes(tmp_path, monkeypatch):
    from check_invariants import check

    stale_pr(tmp_path, monkeypatch, ["FACT-19"], ["FACT-20"])
    check(records_only=True, target="main")


def test_existing_and_moved_ids_are_not_additions(tmp_path, monkeypatch):
    from check_invariants import check

    stale_pr(tmp_path, monkeypatch, ["FACT-19"], ["FACT-20"])
    # DEMO-1 is on both sides from the merge base; the PR moves it to a new file.
    Path("INVARIANTS.yaml").unlink()
    write_rows("pr/INVARIANTS.yaml", "FACT-19", "DEMO-1")
    git("add", "-A")
    git("commit", "-qm", "move")
    check(records_only=True, target="main")


def test_unreadable_target_file_is_skipped(tmp_path, monkeypatch):
    from check_invariants import check

    stale_pr(tmp_path, monkeypatch, ["FACT-19"], ["FACT-20"])
    git("checkout", "-q", "main")
    Path("main/INVARIANTS.yaml").write_text("invariants: [\n")
    git("commit", "-qam", "break")
    git("checkout", "-q", "pr")
    check(records_only=True, target="main")
