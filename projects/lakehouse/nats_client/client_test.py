"""Hermetic tests for the lakehouse NATS JetStream client wrapper.

No real NATS connection: ``nats.connect`` and the JetStream context are mocked.
Coroutines are driven synchronously via ``asyncio.run`` so the suite needs no
``pytest_asyncio`` plugin (and so no extra pip dep).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import projects.lakehouse.nats_client.client as client_module
from projects.lakehouse.nats_client.client import (
    DEFAULT_URL,
    MSG_ID_HEADER,
    NatsClient,
    resolve_url,
)


# --- resolve_url ----------------------------------------------------------


def test_resolve_url_defaults_when_env_empty():
    assert resolve_url({}) == DEFAULT_URL


def test_resolve_url_defaults_when_blank():
    # Whitespace-only is treated as unset.
    assert resolve_url({"NATS_URL": "   "}) == DEFAULT_URL


def test_resolve_url_env_override():
    override = "nats://example.test:4222"
    assert resolve_url({"NATS_URL": override}) == override


def test_resolve_url_strips_whitespace():
    assert resolve_url({"NATS_URL": "  nats://x:4222  "}) == "nats://x:4222"


# --- connect --------------------------------------------------------------


def test_connect_uses_resolved_url():
    mock_nc = MagicMock()
    mock_js = AsyncMock()
    mock_nc.jetstream.return_value = mock_js
    connect = AsyncMock(return_value=mock_nc)

    override = "nats://override.test:4222"
    nc = NatsClient(env={"NATS_URL": override})
    assert nc.url == override

    with patch.object(client_module.nats, "connect", connect):
        asyncio.run(nc.connect())

    connect.assert_awaited_once_with(override)
    mock_nc.jetstream.assert_called_once_with()
    assert nc.nc is mock_nc
    assert nc.js is mock_js


def test_connect_uses_default_url_when_unset():
    mock_nc = MagicMock()
    mock_nc.jetstream.return_value = AsyncMock()
    connect = AsyncMock(return_value=mock_nc)

    nc = NatsClient(env={})
    assert nc.url == DEFAULT_URL

    with patch.object(client_module.nats, "connect", connect):
        asyncio.run(nc.connect())

    connect.assert_awaited_once_with(DEFAULT_URL)


# --- publish --------------------------------------------------------------


def test_publish_sets_msg_id_header_and_forwards_subject_payload():
    nc = NatsClient(url="nats://test:4222")
    nc.js = AsyncMock()

    asyncio.run(nc.publish("events.knowledge.gap", b"payload", msg_id="gap-42-v1"))

    nc.js.publish.assert_awaited_once()
    args, kwargs = nc.js.publish.call_args
    assert args[0] == "events.knowledge.gap"
    assert args[1] == b"payload"
    assert kwargs["headers"][MSG_ID_HEADER] == "gap-42-v1"


def test_publish_merges_caller_headers_with_msg_id():
    nc = NatsClient(url="nats://test:4222")
    nc.js = AsyncMock()

    asyncio.run(
        nc.publish(
            "events.knowledge.gap",
            b"payload",
            msg_id="gap-7-v3",
            headers={"X-Trace": "abc"},
        )
    )

    _, kwargs = nc.js.publish.call_args
    assert kwargs["headers"]["X-Trace"] == "abc"
    assert kwargs["headers"][MSG_ID_HEADER] == "gap-7-v3"


def test_publish_without_msg_id_sends_no_headers():
    nc = NatsClient(url="nats://test:4222")
    nc.js = AsyncMock()

    asyncio.run(nc.publish("events.ops.alert", b"x"))

    _, kwargs = nc.js.publish.call_args
    # No dedup header forced when none requested.
    assert kwargs["headers"] is None


def test_publish_before_connect_raises():
    nc = NatsClient(url="nats://test:4222")
    with pytest.raises(RuntimeError, match=r"connect\(\)"):
        asyncio.run(nc.publish("s", b"p"))


# --- pull_subscribe / fetch ----------------------------------------------


def test_pull_subscribe_creates_durable_consumer_and_fetches():
    underlying = AsyncMock()
    underlying.fetch.return_value = ["msg1", "msg2"]

    mock_js = AsyncMock()
    mock_js.pull_subscribe.return_value = underlying

    nc = NatsClient(url="nats://test:4222")
    nc.js = mock_js

    sub = asyncio.run(nc.pull_subscribe("events.knowledge.gap", "gap-drain", batch=25))

    mock_js.pull_subscribe.assert_awaited_once_with(
        "events.knowledge.gap", durable="gap-drain"
    )

    msgs = asyncio.run(sub.fetch())
    assert msgs == ["msg1", "msg2"]
    # Default batch from pull_subscribe is used when fetch() omits it.
    underlying.fetch.assert_awaited_once_with(25, timeout=5.0)


def test_pull_subscribe_fanout_uses_last_per_subject_config():
    # Fan-out subscribers (e.g. each Quack pod hot-swapping) pass
    # deliver_last_per_subject + inactive_threshold; the wrapper must translate
    # those into an explicit ConsumerConfig so the server starts the consumer at
    # the latest message per subject and auto-expires it when idle. The default
    # (work-distribution) path must NOT build a config — asserted by the test
    # above still passing.
    from nats.js.api import DeliverPolicy

    underlying = AsyncMock()
    mock_js = AsyncMock()
    mock_js.pull_subscribe.return_value = underlying

    nc = NatsClient(url="nats://test:4222")
    nc.js = mock_js

    asyncio.run(
        nc.pull_subscribe(
            "events.lakehouse.artifact.ready",
            "quack-serving-swap-pod-7",
            deliver_last_per_subject=True,
            inactive_threshold=600.0,
        )
    )

    assert mock_js.pull_subscribe.await_count == 1
    args, kwargs = mock_js.pull_subscribe.await_args
    assert args[0] == "events.lakehouse.artifact.ready"
    assert kwargs["durable"] == "quack-serving-swap-pod-7"
    config = kwargs["config"]
    assert config.durable_name == "quack-serving-swap-pod-7"
    assert config.deliver_policy == DeliverPolicy.LAST_PER_SUBJECT
    assert config.inactive_threshold == 600.0


def test_fetch_batch_override():
    underlying = AsyncMock()
    underlying.fetch.return_value = []

    mock_js = AsyncMock()
    mock_js.pull_subscribe.return_value = underlying

    nc = NatsClient(url="nats://test:4222")
    nc.js = mock_js

    sub = asyncio.run(nc.pull_subscribe("s", "d"))
    asyncio.run(sub.fetch(3))

    underlying.fetch.assert_awaited_once_with(3, timeout=5.0)


# --- close ----------------------------------------------------------------


def test_close_closes_connection():
    nc = NatsClient(url="nats://test:4222")
    nc.nc = AsyncMock()
    asyncio.run(nc.close())
    nc.nc.close.assert_awaited_once_with()


def test_close_noop_when_not_connected():
    nc = NatsClient(url="nats://test:4222")
    # Must not raise when never connected.
    asyncio.run(nc.close())
