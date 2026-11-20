import base64
import json
import os
from pathlib import Path
import runpy
from urllib.error import HTTPError

import pytest

module = runpy.run_path(
    os.getenv("SCRIPT", Path(__file__).parent / "files/leaderboard.py")
)
snapshot = module["snapshot"]
ROW = dict(arm="control", n=8, merges=4, reverts=0, median_rounds=1, median_minutes=3)


def test_public_shape():
    [arm] = snapshot([ROW])
    assert set(arm) == {
        "arm",
        "n",
        "median_minutes",
        "median_rounds",
        "merge_rate",
        "revert_rate",
    }
    for key in ("merge_rate", "revert_rate"):
        assert set(arm[key]) == {"value", "low", "high"}
    assert arm["merge_rate"] == {"value": 0.5, "low": 0.21522, "high": 0.78478}
    assert snapshot([dict(ROW, merges=0)])[0]["revert_rate"] is None


def test_rejects_private_fields():
    with pytest.raises(ValueError):
        snapshot([dict(ROW, work_item_id=1)])
    with pytest.raises(ValueError):
        snapshot([dict(ROW, median_rounds={"title": "private"})])


def test_publish_proposes_only_the_site_snapshot(monkeypatch):
    publish = module["publish"]
    repo = "https://api.github.com/repos/jomcgi-org/homelab"
    path = "/contents/projects/jomcgi-site/web/src/factory-outcomes.json"
    branch = "leaderboard/oldblob"
    writes = []

    def api(url, token, method="GET", payload=None):
        if (
            url
            == "http://broker/github/leaderboard-token?repository=jomcgi-org/homelab"
        ):
            assert token == "projected"
            return {"token": "installation"}
        assert token == "installation" and url.startswith(repo)
        route = url.removeprefix(repo)
        if method != "GET":
            writes.append((method, route, payload))
            return {}
        if route == "/git/ref/heads/main":
            return {"object": {"sha": "base"}}
        if route == "/git/ref/heads/" + branch:
            raise HTTPError(url, 404, "absent", {}, None)
        if route in (path + "?ref=base", path + "?ref=" + branch):
            return {"sha": "oldblob", "content": base64.b64encode(b"[]")}
        assert route == "/pulls?base=main&head=jomcgi-org:" + branch
        return []

    monkeypatch.setenv("BROKER_URL", "http://broker")
    monkeypatch.setattr(Path, "read_text", lambda self: "projected")
    monkeypatch.setitem(publish.__globals__, "api", api)
    publish([ROW])
    ref, commit, pull = writes
    assert ref == ("POST", "/git/refs", {"ref": "refs/heads/" + branch, "sha": "base"})
    assert commit[:2] == ("PUT", path)
    assert commit[2]["branch"] == branch and commit[2]["sha"] == "oldblob"
    assert json.loads(base64.b64decode(commit[2]["content"])) == snapshot([ROW])
    assert pull[:2] == ("POST", "/pulls")
    assert pull[2]["head"] == branch and pull[2]["base"] == "main"
