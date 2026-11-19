import hashlib
import io
import json
from pathlib import Path

import pytest

from approvals import main

# The connector store test runs these files against the real migration
# (projects/factory/connector/store/storyboard_approvals_test.go). Regenerate
# them from the package root after changing the SQL:
#   python3 approvals.py HOME < testdata/approved_history.json > testdata/approvals.sql
#   python3 approvals.py --verify HOME < testdata/approved_history.json > testdata/approvals_verify.sql
#   echo '{"HOME-6": {"title": "No prose"}}' | python3 approvals.py --allow-empty HOME > testdata/approvals_empty.sql
TESTDATA = Path(__file__).parent / "testdata"
HISTORY = (TESTDATA / "approved_history.json").read_text()


def run(argv, stdin):
    out = io.StringIO()
    main(argv, io.StringIO(stdin), out)
    return out.getvalue()


def refused(argv, stdin):
    """Return the exit message of a refused run, which writes no SQL."""
    out = io.StringIO()
    with pytest.raises(SystemExit) as exited:
        main(argv, io.StringIO(stdin), out)
    assert out.getvalue() == ""
    # sys.exit with a message prints it to stderr and exits with status 1.
    assert isinstance(exited.value.code, str)
    return exited.value.code


@pytest.mark.parametrize(
    "argv, stdin, golden",
    [
        (["HOME"], HISTORY, "approvals.sql"),
        (["--verify", "HOME"], HISTORY, "approvals_verify.sql"),
        (
            ["--allow-empty", "HOME"],
            '{"HOME-6": {"title": "No prose"}}',
            "approvals_empty.sql",
        ),
    ],
)
def test_output_matches_the_sql_the_store_test_runs(argv, stdin, golden):
    assert run(argv, stdin) == (TESTDATA / golden).read_text()


def test_import_replaces_project_approvals_and_checks_them_before_commit():
    lines = run(["HOME"], HISTORY).splitlines()
    # The publisher hashes the same UTF-8 bytes; duplicates collapse.
    cafe = hashlib.sha256("Café".encode()).hexdigest()
    done = hashlib.sha256(b"Done").hexdigest()
    reviewed = hashlib.sha256(b"Reviewed").hexdigest()
    assert lines[:3] == [
        "SET standard_conforming_strings = on;",
        "BEGIN;",
        "DELETE FROM public.storyboard_approved_message WHERE itsaplan_project = 'HOME';",
    ]
    inserts = [line for line in lines if line.startswith("INSERT")]
    assert inserts == lines[3:6]
    assert sorted(inserts) == sorted(
        "INSERT INTO public.storyboard_approved_message "
        "(itsaplan_project, issue_key, session_id, task_id, text_sha256) "
        f"VALUES ('HOME', {issue}, {sha});"
        for issue, sha in (
            ("'HOME-4', 's''1', 't'", f"'{cafe}'"),
            ("'HOME-4', 's''1', 't'", f"'{done}'"),
            ("'HOME-5', 's2', 'review'", f"'{reviewed}'"),
        )
    )
    # The check runs after every write and before the only COMMIT, so a
    # truncated input never commits.
    check = lines[6:-1]
    assert check[0] == "DO $check$" and check[-1] == "$check$;"
    assert lines[-1] == "COMMIT;" and lines.count("COMMIT;") == 1
    body = "\n".join(check)
    assert "<> 3" in body and "RAISE EXCEPTION" in body
    for sha in (cafe, done, reviewed):
        assert sha in body
    assert "Café" not in "\n".join(lines)


def test_verify_is_a_read_only_check():
    lines = run(["--verify", "HOME"], HISTORY).splitlines()
    assert lines[:3] == [
        "SET standard_conforming_strings = on;",
        "BEGIN TRANSACTION READ ONLY;",
        "DO $check$",
    ]
    assert lines[-1] == "COMMIT;"
    assert not any(word in "\n".join(lines) for word in ("INSERT", "DELETE"))


@pytest.mark.parametrize("argv", [["HOME"], ["--verify", "HOME"]])
@pytest.mark.parametrize(
    "stdin",
    ["{}", '{"HOME-6": {"title": "No prose"}}', '{"HOME-6": {"tasks": []}}'],
)
def test_empty_approval_set_needs_the_flag(argv, stdin):
    assert (
        refused(argv, stdin)
        == "approvals.py: no approved messages; pass --allow-empty to withdraw all"
    )
    lines = run(["--allow-empty", *argv], stdin).splitlines()
    assert "<> 0 THEN" in "\n".join(lines)
    assert not any(line.startswith("INSERT") for line in lines)


def task(**fields):
    return dict(
        dict(session="s", task="t", at="2026-11-02T12:00:00Z", assistantTexts=["x"]),
        **fields,
    )


MALFORMED = {
    "empty input": "",
    "truncated JSON": HISTORY[: len(HISTORY) // 2],
    "not JSON": "op: item not found",
    "not an object": "[]",
    "other project": json.dumps({"MONO-1": {"tasks": [task()]}}),
    "not an issue key": json.dumps({"HOME": {"tasks": [task()]}}),
    "entry not an object": json.dumps({"HOME-4": []}),
    "unknown entry field": json.dumps({"HOME-4": {"titel": "x"}}),
    "title not text": json.dumps({"HOME-4": {"title": 1}}),
    "tasks not a list": json.dumps({"HOME-4": {"tasks": task()}}),
    "task not an object": json.dumps({"HOME-4": {"tasks": ["t"]}}),
    "missing field": json.dumps(
        {"HOME-4": {"tasks": [{"session": "s", "task": "t", "assistantTexts": ["x"]}]}}
    ),
    "unknown task field": json.dumps({"HOME-4": {"tasks": [task(note="x")]}}),
    "session not text": json.dumps({"HOME-4": {"tasks": [task(session=1)]}}),
    "empty task": json.dumps({"HOME-4": {"tasks": [task(task="")]}}),
    "bad time": json.dumps({"HOME-4": {"tasks": [task(at="yesterday")]}}),
    "time without zone": json.dumps(
        {"HOME-4": {"tasks": [task(at="2026-11-02T12:00:00")]}}
    ),
    "no messages": json.dumps({"HOME-4": {"tasks": [task(assistantTexts=[])]}}),
    "messages not a list": json.dumps(
        {"HOME-4": {"tasks": [task(assistantTexts="x")]}}
    ),
    "message not text": json.dumps({"HOME-4": {"tasks": [task(assistantTexts=[1])]}}),
    "message with U+0000": json.dumps(
        {"HOME-4": {"tasks": [task(assistantTexts=["a\x00b"])]}}
    ),
    "dollar-quote tag": json.dumps({"HOME-4": {"tasks": [task(session="$check$")]}}),
}


@pytest.mark.parametrize("stdin", MALFORMED.values(), ids=MALFORMED.keys())
@pytest.mark.parametrize("flags", [[], ["--allow-empty"], ["--verify"]])
def test_bad_input_exits_non_zero_without_sql(stdin, flags):
    assert refused([*flags, "HOME"], stdin).startswith("approvals.py: ")


@pytest.mark.parametrize("project", ["", "home", "HO'ME", "HOME-1"])
def test_bad_project_key_exits_non_zero(project):
    assert refused([project], HISTORY).startswith("approvals.py: ")
