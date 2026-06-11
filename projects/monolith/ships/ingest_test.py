"""Tests for the supervised AISStream ingest loop (ships.ingest).

The ingest loop is the only always-on networked component of the ships module.
These tests prove it:
  1. parses messages and flushes the batch (here via the clean-close remainder),
  2. backs off on reconnect and resets the delay after a success,
  3. never propagates a loop-body exception (cannot crash the app),
  4. disables itself (no connect) when AISSTREAM_API_KEY is unset.

Every test sets the stop Event (usually inside a patched asyncio.sleep) so the
supervised loop always terminates: no test makes a real network call or hangs.
The websocket-mock approach mirrors projects/ships/ingest/reconnect_cap_test.py.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, patch

import pytest

import ships.ingest as ingest
from ships.ingest import (
    INITIAL_RECONNECT_DELAY,
    RECONNECT_BACKOFF_FACTOR,
    ais_stream_loop,
)


# ---------------------------------------------------------------------------
# Websocket mocks
# ---------------------------------------------------------------------------


class _ScriptedWS:
    """Async-context-manager websocket that yields a fixed list of raw messages."""

    def __init__(self, messages):
        self._messages = list(messages)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def send(self, _msg):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


class _FailOnEnterWS:
    """Websocket whose connect (__aenter__) always raises a generic error."""

    async def __aenter__(self):
        raise RuntimeError("connection refused")

    async def __aexit__(self, *args):
        return False


class _RaiseDuringIterWS:
    """Websocket that raises a generic exception inside the message loop body."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def send(self, _msg):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise RuntimeError("boom inside the loop")


def _position_raw(mmsi: int = 123456789) -> str:
    return json.dumps(
        {
            "MessageType": "PositionReport",
            "MetaData": {
                "MMSI": mmsi,
                "ShipName": "TEST",
                "time_utc": "2024-01-15T10:00:00Z",
            },
            "Message": {
                "PositionReport": {
                    "Latitude": 48.0,
                    "Longitude": -123.0,
                    "Sog": 5.0,
                    "Cog": 90.0,
                    "TrueHeading": 91,
                    "NavigationalStatus": 0,
                }
            },
        }
    )


def _vessel_raw(mmsi: int = 123456789) -> str:
    return json.dumps(
        {
            "MessageType": "ShipStaticData",
            "MetaData": {
                "MMSI": mmsi,
                "ShipName": "TEST",
                "time_utc": "2024-01-15T10:00:00Z",
            },
            "Message": {
                "ShipStaticData": {
                    "Name": "TEST",
                    "Type": 70,
                    "ImoNumber": 9999999,
                    "CallSign": "ABCD",
                    "Destination": "PORT",
                    "Dimension": {"A": 10, "B": 20, "C": 5, "D": 5},
                    "MaximumStaticDraught": 8.0,
                }
            },
        }
    )


# ---------------------------------------------------------------------------
# 1. Parsed messages reach the flush
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_messages_are_parsed_and_flushed():
    """One PositionReport + one ShipStaticData are flushed as parsed rows."""
    stop = asyncio.Event()
    flushed: list[tuple[list, list]] = []

    def recorder(positions, vessels):
        # Snapshot copies (the loop reuses/clears its lists after the call).
        flushed.append((list(positions), list(vessels)))

    ws = _ScriptedWS([_position_raw(), _vessel_raw()])

    async def fake_sleep(_delay):
        # Reached only after the clean close + remainder flush; end the loop.
        stop.set()

    with (
        patch.dict("os.environ", {"AISSTREAM_API_KEY": "test-key"}),
        patch("ships.ingest.ssl.create_default_context", return_value=MagicMock()),
        patch("websockets.connect", return_value=ws),
        patch("ships.ingest._flush", side_effect=recorder),
        patch("ships.ingest.asyncio.sleep", side_effect=fake_sleep),
    ):
        await ais_stream_loop(stop)

    assert len(flushed) == 1, f"expected one flush, got {len(flushed)}"
    positions, vessels = flushed[0]
    assert [p["mmsi"] for p in positions] == ["123456789"]
    assert [v["mmsi"] for v in vessels] == ["123456789"]
    assert positions[0]["lat"] == 48.0
    assert vessels[0]["name"] == "TEST"


# ---------------------------------------------------------------------------
# 2. Backoff grows on failure, resets on success
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backoff_grows_then_resets_on_success():
    """Reconnect delay doubles across failures and resets after a connect."""
    stop = asyncio.Event()
    delays: list[float] = []

    # fail, fail, then a clean (empty) connection that resets the backoff.
    connections = [_FailOnEnterWS(), _FailOnEnterWS(), _ScriptedWS([])]

    async def fake_sleep(delay):
        delays.append(delay)
        if len(delays) >= 3:
            stop.set()

    with (
        patch.dict("os.environ", {"AISSTREAM_API_KEY": "test-key"}),
        patch("ships.ingest.ssl.create_default_context", return_value=MagicMock()),
        patch("websockets.connect", side_effect=connections),
        patch("ships.ingest.asyncio.sleep", side_effect=fake_sleep),
    ):
        await ais_stream_loop(stop)

    # 1.0 (after fail), 2.0 (grew, after second fail), 1.0 (reset after success).
    assert delays[0] == INITIAL_RECONNECT_DELAY
    assert delays[1] == pytest.approx(
        INITIAL_RECONNECT_DELAY * RECONNECT_BACKOFF_FACTOR
    )
    assert delays[2] == INITIAL_RECONNECT_DELAY


# ---------------------------------------------------------------------------
# 3. A loop-body exception never propagates (the app cannot be crashed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_loop_body_exception_does_not_propagate():
    """An exception raised mid-iteration is swallowed; the coroutine returns."""
    stop = asyncio.Event()

    async def fake_sleep(_delay):
        stop.set()

    with (
        patch.dict("os.environ", {"AISSTREAM_API_KEY": "test-key"}),
        patch("ships.ingest.ssl.create_default_context", return_value=MagicMock()),
        patch("websockets.connect", return_value=_RaiseDuringIterWS()),
        patch("ships.ingest.asyncio.sleep", side_effect=fake_sleep),
    ):
        # Must not raise: the supervised loop swallows the error and retries.
        await ais_stream_loop(stop)

    assert stop.is_set()


# ---------------------------------------------------------------------------
# 4. Unset API key disables ingest (no connect attempt)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unset_api_key_disables_ingest():
    """With AISSTREAM_API_KEY empty, the loop returns without connecting."""
    stop = asyncio.Event()
    connect = MagicMock()

    with (
        patch.dict("os.environ", {"AISSTREAM_API_KEY": ""}),
        patch("websockets.connect", connect),
    ):
        await ais_stream_loop(stop)

    connect.assert_not_called()
