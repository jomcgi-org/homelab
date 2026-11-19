"""Print SQL that records the owner's approved messages in the connector store.

The storyboard_task_messages view returns a digest to the publisher only for
a row in storyboard_approved_message. The publisher login cannot read or
write that table. The owner runs this script on APPROVED_HISTORY and gives
its output to psql in the factory_connector database (README.md). The output
replaces every approval for the project, so a withdrawn message also loses
its approval.

Any bad input exits non-zero before the script prints SQL. An empty approval
set needs --allow-empty. Inside its transaction, the SQL checks that the
table holds exactly the expected rows and raises on a mismatch. It commits
only in its last statement, so a truncated input rolls back. --verify prints
the same check as a read-only transaction for use after the import.
"""

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime

TABLE = "public.storyboard_approved_message"
ENTRY_KEYS = {"title", "description", "tasks"}
TASK_KEYS = {"session", "task", "at", "assistantTexts"}


def literal(value):
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError("approval fields must be text")
    return "'" + value.replace("'", "''") + "'"


def text(value, name):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{name} must be non-empty text without U+0000")
    return value


def approved_rows(project, history):
    """Return the sorted (issue, session, task, digest) rows of a valid history."""
    if not re.fullmatch(r"[A-Z][A-Z0-9]*", project):
        raise ValueError("the project key must be upper-case letters and digits")
    if not isinstance(history, dict):
        raise ValueError("APPROVED_HISTORY must be a JSON object")
    rows = set()
    for issue_key, approval in history.items():
        if not re.fullmatch(re.escape(project) + r"-[1-9][0-9]*", issue_key):
            raise ValueError(f"{issue_key!r} is not an issue key of {project}")
        if not isinstance(approval, dict) or not approval.keys() <= ENTRY_KEYS:
            raise ValueError(f"{issue_key}: unexpected approval entry")
        for name in ("title", "description"):
            if name in approval:
                text(approval[name], f"{issue_key} {name}")
        tasks = approval.get("tasks", [])
        if not isinstance(tasks, list):
            raise ValueError(f"{issue_key}: tasks must be a list")
        for number, task in enumerate(tasks):
            where = f"{issue_key} task {number}"
            if not isinstance(task, dict) or task.keys() != TASK_KEYS:
                raise ValueError(f"{where}: expected exactly {sorted(TASK_KEYS)}")
            session = text(task["session"], f"{where} session")
            task_id = text(task["task"], f"{where} task")
            at = datetime.fromisoformat(text(task["at"], f"{where} at"))
            if at.tzinfo is None:
                raise ValueError(f"{where}: at has no timezone")
            texts = task["assistantTexts"]
            if not isinstance(texts, list) or not texts:
                raise ValueError(f"{where}: assistantTexts must be a non-empty list")
            for message in texts:
                text(message, f"{where} message")
                digest = hashlib.sha256(message.encode()).hexdigest()
                rows.add((issue_key, session, task_id, digest))
    return sorted(rows)


def check(project, rows):
    """A DO block that raises unless the table holds exactly these rows."""
    scope = f"FROM {TABLE} WHERE itsaplan_project = {literal(project)}"
    mismatch = f"(SELECT count(*) {scope}) <> {len(rows)}"
    if rows:
        expected = ",\n      ".join(
            "(" + ", ".join(literal(v) for v in row) + ")" for row in rows
        )
        mismatch += (
            "\n     OR EXISTS (\n"
            f"      SELECT issue_key, session_id, task_id, text_sha256 {scope}\n"
            "      EXCEPT VALUES\n"
            f"      {expected})"
        )
    body = (
        "BEGIN\n"
        f"  IF {mismatch} THEN\n"
        f"    RAISE EXCEPTION 'storyboard approvals for % do not match the "
        f"expected % messages', {literal(project)}, {len(rows)};\n"
        "  END IF;\n"
        f"  RAISE NOTICE 'storyboard approvals for %: % messages match', "
        f"{literal(project)}, {len(rows)};\n"
        "END"
    )
    if "$check$" in body:
        raise ValueError("approval fields must not contain $check$")
    return f"DO $check$\n{body}\n$check$;"


def statements(project, rows):
    yield "SET standard_conforming_strings = on;"
    yield "BEGIN;"
    yield f"DELETE FROM {TABLE} WHERE itsaplan_project = {literal(project)};"
    for row in rows:
        values = ", ".join(literal(v) for v in (project, *row))
        yield (
            f"INSERT INTO {TABLE} "
            "(itsaplan_project, issue_key, session_id, task_id, text_sha256) "
            f"VALUES ({values});"
        )
    yield check(project, rows)
    yield "COMMIT;"


def verification(project, rows):
    yield "SET standard_conforming_strings = on;"
    yield "BEGIN TRANSACTION READ ONLY;"
    yield check(project, rows)
    yield "COMMIT;"


def main(argv=None, stdin=sys.stdin, stdout=sys.stdout):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("project")
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="accept an approval set with no messages, which withdraws them all",
    )
    parser.add_argument(
        "--verify", action="store_true", help="print only the read-only check"
    )
    args = parser.parse_args(argv)
    try:
        rows = approved_rows(args.project, json.loads(stdin.read()))
        if not rows and not args.allow_empty:
            raise ValueError("no approved messages; pass --allow-empty to withdraw all")
        lines = (verification if args.verify else statements)(args.project, rows)
        sql = "\n".join(lines) + "\n"
    except (ValueError, UnicodeError) as err:
        sys.exit(f"approvals.py: {err}")
    print(
        f"approvals.py: {len(rows)} approved messages for {args.project}",
        file=sys.stderr,
    )
    stdout.write(sql)


if __name__ == "__main__":
    main()
