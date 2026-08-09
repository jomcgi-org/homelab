import httpx
import pytest

import graph.steps as steps


class FakeClient:
    response = None

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, headers):
        self.url = url
        self.headers = headers
        return self.response


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [(200, {"object": {"sha": "deadbeef"}}, "deadbeef"), (404, {}, None)],
)
def test_read_branch_head(monkeypatch, status, payload, expected):
    request = httpx.Request(
        "GET", "https://api.github.com/repos/jomcgi/homelab/git/ref/heads/graph/wf-1"
    )
    response = httpx.Response(status, json=payload, request=request)
    FakeClient.response = response
    monkeypatch.setattr(steps.httpx, "Client", FakeClient)

    assert (
        steps.read_branch_head.__wrapped__("jomcgi/homelab", "graph/wf-1") == expected
    )


def test_read_branch_head_raises_on_server_error(monkeypatch):
    request = httpx.Request(
        "GET", "https://api.github.com/repos/jomcgi/homelab/git/ref/heads/graph/wf-1"
    )
    FakeClient.response = httpx.Response(500, json={"message": "boom"}, request=request)
    monkeypatch.setattr(steps.httpx, "Client", FakeClient)

    with pytest.raises(httpx.HTTPStatusError):
        steps.read_branch_head.__wrapped__("jomcgi/homelab", "graph/wf-1")


def test_start_agent_session_forwards_workflow_id(monkeypatch):
    import agent_sessions.api as api

    calls = []

    def fake_start_session(*args, **kwargs):
        calls.append((args, kwargs))
        return 101

    monkeypatch.setattr(api, "start_session_for_graph", fake_start_session)

    result = steps.start_agent_session.__wrapped__(
        "test-key", "prompt", "luna", "jomcgi/homelab", "main", workflow_id="wf-abc"
    )

    assert result == 101
    assert calls == [
        (
            ("test-key", "prompt", "luna", "jomcgi/homelab", "main"),
            {"workflow_id": "wf-abc"},
        )
    ]
