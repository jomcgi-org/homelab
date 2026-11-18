from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
from unittest.mock import Mock

from publisher import public_issue, public_turn, publish


def test_visibility_and_whole_turn_withholding():
    assert public_issue({"project": "HOME", "labels": []}, {"HOME"})
    assert not public_issue({"project": "OTHER", "labels": []}, {"HOME"})
    assert not public_issue({"project": "HOME", "labels": ["private"]}, {"HOME"})
    turn = dict(number=2, role="assistant", at="2026-11-18T09:00:00Z")
    assert public_turn(dict(turn, completed=False), []) is None
    complete = dict(
        turn, completed=True, text="a@test.dev user-7", toolCalls=[], diff="secret"
    )
    assert (
        public_turn(complete, ["user-7"], lambda _: False)["text"]
        == "[redacted] [redacted]"
    )
    assert public_turn(complete, [], lambda data: b"secret" in data) == dict(
        turn, withheld=True
    )


def test_commit_and_retention():
    s3 = Mock()
    now = datetime.now(UTC)
    old, recent = ("objects/" + c * 64 + ".json" for c in "ab")
    s3.get_paginator.return_value.paginate.return_value = [
        {
            "Contents": [
                {"Key": old, "LastModified": now - timedelta(hours=24)},
                {"Key": recent, "LastModified": now - timedelta(hours=23, minutes=59)},
            ]
        }
    ]
    publish(s3, "factory-public", now, {"kind": "story"}, {}, {}, {}, lambda _: False)
    obj, manifest = [call.kwargs for call in s3.put_object.call_args_list]
    assert obj["Key"] == "objects/" + hashlib.sha256(obj["Body"]).hexdigest() + ".json"
    assert manifest["Key"] == "manifest.json"
    assert json.loads(manifest["Body"])["story"] == obj["Key"]
    s3.delete_object.assert_called_once_with(Bucket="factory-public", Key=old)


def test_real_gitleaks_hit_withholds_entire_turn(monkeypatch):
    binary = os.environ.get("GITLEAKS_BIN")
    if binary:
        monkeypatch.setenv(
            "PATH", str(Path(binary).resolve().parent) + os.pathsep + os.environ["PATH"]
        )
    header = dict(number=1, at="2026-11-18T09:00:00Z", role="assistant")
    turn = dict(header, completed=True, text="sample output", toolCalls=[], diff="")
    assert public_turn(turn, [])["withheld"] is False
    turn["diff"] = "ghp_" + "6rQ0cV2qN5mHt8ZkFx9AbR3WgY4DuL7PjE1s"
    assert public_turn(turn, []) == dict(header, withheld=True)
