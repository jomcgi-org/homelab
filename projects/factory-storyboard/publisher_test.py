from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from publisher import public_issue, public_texts, public_turns, publish

SECRET = "ghp_" + "6rQ0cV2qN5mHt8ZkFx9AbR3WgY4DuL7PjE1s"


def matching(word):
    return lambda bodies: {i for i, body in enumerate(bodies) if word in body}


@pytest.fixture
def real_gitleaks(monkeypatch):
    binary = os.environ.get("GITLEAKS_BIN")
    if binary:
        monkeypatch.setenv(
            "PATH",
            str(Path(binary).resolve(strict=True).parent)
            + os.pathsep
            + os.environ["PATH"],
        )


@pytest.mark.parametrize(
    "label",
    ["private", "Private", "security", "Security", "INTSEC", "security-finding"],
)
def test_private_and_security_labels_withhold_the_issue(label):
    assert public_issue({"project": "HOME", "labels": ["deferred"]}, {"HOME"})
    assert not public_issue({"project": "OTHER", "labels": []}, {"HOME"})
    assert not public_issue(
        {"project": "HOME", "labels": ["deferred", label]}, {"HOME"}
    )


def test_redaction_and_whole_turn_withholding():
    turn = dict(number=2, role="assistant", at="2026-11-18T09:00:00Z")
    complete = dict(turn, text="a@test.dev user-7", toolCalls=[], diff="secret")
    (public,) = public_turns([complete], ["user-7"], lambda _: set())
    assert public["text"] == "[redacted] [redacted]"
    assert public["withheld"] is False
    clean = dict(complete, number=3, text="plain", diff="")
    assert public_turns([complete, clean], [], matching(b"secret")) == [
        dict(turn, withheld=True),
        dict(clean, withheld=False),
    ]
    assert public_texts(
        ["user-7 shipped", "a secret"], ["user-7"], matching(b"secret")
    ) == [
        "[redacted] shipped",
        None,
    ]


def test_commit_and_retention():
    s3 = Mock()
    now = datetime.now(UTC)
    before_outage = now - timedelta(hours=24)
    s3.get_paginator.return_value.paginate.return_value = []
    publish(
        s3,
        "factory-public",
        before_outage,
        {"old": True},
        {},
        {},
        {},
        matching(b"leak"),
    )
    old = json.loads(s3.put_object.call_args.kwargs["Body"])["story"]
    s3.reset_mock()
    recent = "objects/" + "b" * 64 + ".json"
    s3.get_paginator.return_value.paginate.return_value = [
        {
            "Contents": [
                {"Key": old, "LastModified": before_outage},
                {"Key": recent, "LastModified": now - timedelta(hours=23, minutes=59)},
            ]
        }
    ]
    s3.head_object.return_value = {"Metadata": {}}
    scanned = []

    def scan(bodies):
        scanned.append(bodies)
        return set()

    publish(s3, "factory-public", now, {"kind": "story"}, {}, {}, {}, scan)
    obj, manifest = [call.kwargs for call in s3.put_object.call_args_list]
    assert obj["Key"] == "objects/" + hashlib.sha256(obj["Body"]).hexdigest() + ".json"
    assert manifest["Key"] == "manifest.json"
    assert json.loads(manifest["Body"])["story"] == obj["Key"]
    # One scan covers every object and the manifest.
    assert scanned == [[obj["Body"], manifest["Body"]]]
    s3.delete_object.assert_not_called()
    mark = s3.copy_object.call_args.kwargs
    assert mark["Key"] == old
    assert mark["CopySource"] == {"Bucket": "factory-public", "Key": old}
    assert mark["MetadataDirective"] == "REPLACE"

    # The post-outage copy starts a new LastModified interval.
    s3.head_object.return_value = {"Metadata": mark["Metadata"]}
    s3.get_paginator.return_value.paginate.return_value = [
        {"Contents": [{"Key": old, "LastModified": now}]}
    ]
    publish(s3, "factory-public", now + timedelta(seconds=989), {}, {}, {}, {}, scan)
    s3.delete_object.assert_not_called()
    publish(s3, "factory-public", now + timedelta(hours=24), {}, {}, {}, {}, scan)
    s3.delete_object.assert_called_once_with(Bucket="factory-public", Key=old)


def test_secret_in_any_object_writes_nothing():
    s3 = Mock()
    sessions = {"session-1": {"kind": "session", "text": "leak"}}
    with pytest.raises(ValueError, match="secret detected"):
        publish(s3, "b", datetime.now(UTC), {}, {}, {}, sessions, matching(b"leak"))
    s3.put_object.assert_not_called()
    s3.delete_object.assert_not_called()


def test_real_gitleaks_hit_withholds_only_the_matching_turn(real_gitleaks):
    header = dict(at="2026-11-18T09:00:00Z", role="assistant")
    turns = [
        dict(header, number=n, text=f"sample output {n}", toolCalls=[], diff="")
        for n in range(1, 4)
    ]
    turns[1]["diff"] = SECRET
    assert public_turns(turns, []) == [
        dict(turns[0], withheld=False),
        dict(header, number=2, withheld=True),
        dict(turns[2], withheld=False),
    ]
    assert public_texts(["Ship it", "token " + SECRET], []) == ["Ship it", None]
    assert public_turns([], []) == []


def test_real_gitleaks_stops_a_publication(real_gitleaks):
    s3 = Mock()
    with pytest.raises(ValueError, match="secret detected"):
        publish(s3, "b", datetime.now(UTC), {"text": SECRET}, {}, {}, {})
    s3.put_object.assert_not_called()


def test_real_gitleaks_ignores_inline_allow_comments(real_gitleaks):
    # Each scan input is one JSON line, so a gitleaks:allow anywhere in the
    # published text would otherwise hide every finding in that input.
    allowed = f"token {SECRET} # gitleaks:allow"
    header = dict(at="2026-11-18T09:00:00Z", role="assistant")
    turns = [
        dict(header, number=1, text=allowed, toolCalls=[], diff=""),
        dict(header, number=2, text="gitleaks:allow", toolCalls=[], diff=SECRET),
        dict(header, number=3, text="clean gitleaks:allow", toolCalls=[], diff=""),
    ]
    assert public_turns(turns, []) == [
        dict(header, number=1, withheld=True),
        dict(header, number=2, withheld=True),
        dict(turns[2], withheld=False),
    ]
    assert public_texts([allowed, "Ship it"], []) == [None, "Ship it"]
    s3 = Mock()
    sessions = {"session-1": {"kind": "session", "text": allowed}}
    with pytest.raises(ValueError, match="secret detected"):
        publish(s3, "b", datetime.now(UTC), {}, {}, {}, sessions)
    s3.put_object.assert_not_called()


def test_shared_task_read_checks_session_and_completion():
    from io import BytesIO
    from session_history import completed_task

    task = {
        "id": "task",
        "contextId": "session",
        "status": {"state": "TASK_STATE_COMPLETED"},
    }
    opener = Mock()

    def respond(request, timeout):
        assert request.get_header("Authorization") == "Bearer visitor"
        assert request.get_header("X-share-token") == "per-session"
        assert json.loads(request.data)["method"] == "GetTask"
        return BytesIO(
            json.dumps({"jsonrpc": "2.0", "id": "task", "result": task}).encode()
        )

    opener.open.side_effect = respond

    def read():
        return completed_task(
            "https://gateway/agents/ns/coder",
            "session",
            "task",
            "visitor",
            "per-session",
            opener=opener,
        )

    assert read() == task
    task["status"]["state"] = "TASK_STATE_WORKING"
    assert read() is None
    task["contextId"] = "another-session"
    with pytest.raises(ValueError, match="different task or session"):
        read()
