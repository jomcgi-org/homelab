import hashlib

import pytest

from approvals import statements


def test_statements_replace_project_approvals():
    history = {
        "HOME-4": {
            "title": "Ship it",
            "tasks": [
                {
                    "session": "s'1",
                    "task": "t",
                    "at": "2026-11-02T12:00:00Z",
                    "assistantTexts": ["Café", "Café", "Done"],
                }
            ],
        },
        "HOME-5": {"title": "No prose"},
    }
    lines = list(statements("HOME", history))
    # The publisher hashes the same UTF-8 bytes.
    cafe = hashlib.sha256("Café".encode()).hexdigest()
    done = hashlib.sha256(b"Done").hexdigest()
    assert lines[:3] == [
        "SET standard_conforming_strings = on;",
        "BEGIN;",
        "DELETE FROM public.storyboard_approved_message WHERE itsaplan_project = 'HOME';",
    ]
    assert lines[-1] == "COMMIT;"
    inserts = lines[3:-1]
    assert sorted(inserts) == sorted(
        "INSERT INTO public.storyboard_approved_message "
        "(itsaplan_project, issue_key, session_id, task_id, text_sha256) "
        f"VALUES ('HOME', 'HOME-4', 's''1', 't', '{sha}');"
        for sha in (cafe, done)
    )
    assert "Café" not in "".join(lines)


def test_empty_history_withdraws_every_approval():
    assert list(statements("HOME", {}))[1:] == [
        "BEGIN;",
        "DELETE FROM public.storyboard_approved_message WHERE itsaplan_project = 'HOME';",
        "COMMIT;",
    ]


def test_rejects_non_text_fields():
    history = {
        "HOME-4": {"tasks": [{"session": 1, "task": "t", "assistantTexts": ["x"]}]}
    }
    with pytest.raises(ValueError):
        list(statements("HOME", history))
