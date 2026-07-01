"""Unit tests for the goosecracker delivery/publish path (ADR 024).

Covers the artifact publish + Discord message shaping: an artifact run publishes
the built HTML and posts a clean "Artifact ready: <url>" (with the recipe summary)
rather than the raw goose transcript; a whiffed artifact build and a publish
failure both get clear messages.
"""

from __future__ import annotations

from goosecracker import runner

_GOOSE_RESULT = (
    "  ▸ write path /tmp/artifact.html\n"
    "```goose-result\n"
    "type: note\n"
    "summary: A bouncing ball demo with gravity.\n"
    "```\n"
)


def test_extract_summary_pulls_summary_line():
    assert (
        runner._extract_summary(_GOOSE_RESULT) == "A bouncing ball demo with gravity."
    )


def test_extract_summary_empty_when_absent():
    assert runner._extract_summary("no result block here") == ""


async def test_delivery_message_publishes_and_links(monkeypatch):
    monkeypatch.setattr(
        runner, "_publish_artifact", lambda s, h: "https://jomcgi.dev/artifact/abc123"
    )
    data = {"artifactHtml": "<html>x</html>", "result": _GOOSE_RESULT}

    msg = await runner._delivery_message("sess", "artifact", data)

    assert "https://jomcgi.dev/artifact/abc123" in msg
    assert "A bouncing ball demo with gravity." in msg
    # the raw transcript (the write tool line) must NOT be dumped
    assert "▸ write path" not in msg


async def test_delivery_message_artifact_run_with_no_artifact():
    msg = await runner._delivery_message(
        "sess", "artifact", {"result": "chatted instead"}
    )
    assert "no artifact" in msg.lower()


async def test_delivery_message_publish_failure_is_reported(monkeypatch):
    def boom(_s, _h):
        raise RuntimeError("s3 down")

    monkeypatch.setattr(runner, "_publish_artifact", boom)
    msg = await runner._delivery_message(
        "sess", "artifact", {"artifactHtml": "<html>x</html>"}
    )
    assert "failed" in msg.lower()


async def test_delivery_message_non_artifact_posts_result():
    msg = await runner._delivery_message("sess", "agent", {"result": "hello from qwen"})
    assert msg == "hello from qwen"
