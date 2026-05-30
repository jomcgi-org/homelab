"""Hermetic tests for the NATS -> Temporal dispatchers (ADR agents/016).

No NATS server, no Temporal server, no network: the NATS client, its pull
subscriptions, the Temporal client, and individual NATS messages are all fakes.
Coroutines are driven with ``asyncio.run``. The only real Temporal symbol used is
``WorkflowAlreadyStartedError`` (an exception type, cheap to construct).
"""

from __future__ import annotations

import asyncio

import pytest
from temporalio.exceptions import WorkflowAlreadyStartedError

from projects.lakehouse.dispatchers import Dispatcher, all_dispatchers
from projects.lakehouse.dispatchers import artifact_ready, gap_ready, run
from projects.lakehouse.events.envelope import EventEnvelope, build_envelope


# --- fakes ----------------------------------------------------------------


class FakeTemporalClient:
    """Records ``start_workflow`` calls; can be told to raise AlreadyStarted."""

    def __init__(self, *, raise_already_started: bool = False) -> None:
        self.calls: list[dict] = []
        self._raise_already_started = raise_already_started

    async def start_workflow(self, workflow, *args, **kwargs):
        self.calls.append({"workflow": workflow, "args": args, "kwargs": kwargs})
        if self._raise_already_started:
            raise WorkflowAlreadyStartedError(kwargs.get("id", "?"), str(workflow))
        return object()  # a fake WorkflowHandle


class FakeMsg:
    """A NATS message that records its ack/term disposition."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.acked = False
        self.termed = False

    async def ack(self) -> None:
        self.acked = True

    async def term(self) -> None:
        self.termed = True


class FakeSubscription:
    """Yields one canned batch of messages, then only times out.

    Mirrors a durable pull subscription's ``fetch``: the first call returns the
    batch; subsequent calls raise ``TimeoutError`` (idle stream) so the run loop
    re-polls until ``stop`` is set.
    """

    def __init__(self, batches: list[list[FakeMsg]]) -> None:
        self._batches = list(batches)

    async def fetch(self, batch=None, *, timeout=None):
        if self._batches:
            return self._batches.pop(0)
        raise TimeoutError


class FakeNatsClient:
    """Hands out pre-seeded subscriptions keyed by ``(subject, durable)``."""

    def __init__(self, subs: dict[tuple[str, str], FakeSubscription]) -> None:
        self._subs = subs
        self.subscribed: list[tuple[str, str]] = []
        self.connected = False
        self.closed = False

    async def connect(self) -> None:
        self.connected = True

    async def pull_subscribe(self, subject, durable, *, batch=10):
        self.subscribed.append((subject, durable))
        return self._subs[(subject, durable)]

    async def close(self) -> None:
        self.closed = True


# --- helpers --------------------------------------------------------------


def _gap_envelope(entity_id: str = "42", event_type: str = "created") -> EventEnvelope:
    return build_envelope(
        entity_type="gap",
        entity_id=entity_id,
        event_type=event_type,
        event_version=1,
        producer="monolith.gardener",
        payload={"term": "wong-zakai", "context": "ctx"},
    )


def _raw(envelope: EventEnvelope) -> bytes:
    return envelope.model_dump_json().encode("utf-8")


# --- discovery (all_dispatchers) ------------------------------------------


def test_all_dispatchers_discovers_both():
    found = all_dispatchers()
    subjects = {d.subject for d in found}
    assert "events.knowledge.gap" in subjects
    assert "events.serving.artifact-ready" in subjects
    # Exactly the two dispatcher modules' single entries each.
    assert len(found) == 2
    assert all(isinstance(d, Dispatcher) for d in found)


def test_discovered_dispatchers_have_distinct_durables():
    durables = [d.durable for d in all_dispatchers()]
    assert len(durables) == len(set(durables)), "durables must be unique"
    # artifact-ready must NOT collide with quack-server's swap durable.
    assert "quack-serving-swap" not in durables


# --- gap_ready dispatcher --------------------------------------------------


def test_gap_ready_dispatcher_shape():
    (d,) = gap_ready.DISPATCHERS
    assert d.subject == "events.knowledge.gap"
    assert d.durable == "gap-drain-dispatcher"
    assert d.event_type == "created"


def test_gap_ready_starts_workflow_with_deterministic_id():
    client = FakeTemporalClient()
    envelope = _gap_envelope("42")
    asyncio.run(gap_ready.handle_gap_created(envelope, client))

    assert len(client.calls) == 1
    call = client.calls[0]
    # Workflow referenced by type-name STRING (not the class).
    assert call["workflow"] == "GapDrainWorkflow"
    assert call["kwargs"]["id"] == "gap-drain-42"
    assert call["kwargs"]["task_queue"] == "gap-drain"
    # Event payload forwarded as the workflow arg.
    assert call["args"][0] == envelope.payload


def test_gap_ready_swallows_already_started():
    client = FakeTemporalClient(raise_already_started=True)
    # Must not raise — WorkflowAlreadyStartedError is an idempotent no-op.
    asyncio.run(gap_ready.handle_gap_created(_gap_envelope("7"), client))
    assert client.calls[0]["kwargs"]["id"] == "gap-drain-7"


def test_workflow_id_for_is_deterministic():
    assert gap_ready.workflow_id_for("42") == "gap-drain-42"
    assert gap_ready.workflow_id_for("42") == gap_ready.workflow_id_for("42")


# --- artifact_ready dispatcher (stub) -------------------------------------


def test_artifact_ready_dispatcher_shape():
    (d,) = artifact_ready.DISPATCHERS
    assert d.subject == "events.serving.artifact-ready"
    assert d.durable == "artifact-ready-dispatcher"
    # No event_type filter — reacts to every event on the subject.
    assert d.event_type is None


def test_artifact_ready_stub_does_not_touch_temporal():
    client = FakeTemporalClient()
    envelope = build_envelope(
        entity_type="serving-artifact",
        entity_id="artifact-2026-05-30",
        event_type="created",
        event_version=1,
        producer="lakehouse.build_serving",
        payload={"artifact_url": "s3://warehouse/serving/v5.duckdb", "version": "v5"},
    )
    asyncio.run(artifact_ready.handle_artifact_ready(envelope, client))
    # Stub must NOT start a workflow — quack-server owns the authoritative swap.
    assert client.calls == []


# --- Dispatcher.matches (event_type filter) -------------------------------


def test_matches_filters_by_event_type():
    (d,) = gap_ready.DISPATCHERS
    assert d.matches(_gap_envelope("1", "created")) is True
    assert d.matches(_gap_envelope("1", "updated")) is False
    assert d.matches(_gap_envelope("1", "tombstoned")) is False


def test_matches_no_filter_accepts_everything():
    (d,) = artifact_ready.DISPATCHERS
    assert d.matches(_gap_envelope("1", "created")) is True
    assert d.matches(_gap_envelope("1", "anything")) is True


# --- run.decode_envelope --------------------------------------------------


def test_decode_envelope_roundtrips():
    envelope = _gap_envelope("99")
    decoded = run.decode_envelope(_raw(envelope))
    assert decoded.entity_id == "99"
    assert decoded.event_type == "created"
    assert decoded.payload == envelope.payload


def test_decode_envelope_rejects_malformed():
    with pytest.raises(Exception):
        run.decode_envelope(b"not json at all")


# --- run.dispatch_message disposition -------------------------------------


def test_dispatch_message_created_event_starts_and_acks():
    (d,) = gap_ready.DISPATCHERS
    client = FakeTemporalClient()
    msg = FakeMsg(_raw(_gap_envelope("42", "created")))
    asyncio.run(run.dispatch_message(d, msg, client))

    assert msg.acked is True
    assert msg.termed is False
    assert client.calls[0]["kwargs"]["id"] == "gap-drain-42"


def test_dispatch_message_filtered_event_acks_without_dispatch():
    (d,) = gap_ready.DISPATCHERS
    client = FakeTemporalClient()
    msg = FakeMsg(_raw(_gap_envelope("42", "updated")))  # not "created"
    asyncio.run(run.dispatch_message(d, msg, client))

    assert msg.acked is True  # filtered-out events are acked, not redelivered
    assert client.calls == []  # handler never invoked


def test_dispatch_message_malformed_is_termed():
    (d,) = gap_ready.DISPATCHERS
    client = FakeTemporalClient()
    msg = FakeMsg(b"{not valid json")
    asyncio.run(run.dispatch_message(d, msg, client))

    assert msg.termed is True
    assert msg.acked is False
    assert client.calls == []


def test_dispatch_message_handler_error_leaves_for_redelivery():
    async def boom(envelope, temporal_client):
        raise RuntimeError("handler blew up")

    d = Dispatcher(
        subject="events.knowledge.gap",
        durable="x",
        handle=boom,
        event_type="created",
    )
    client = FakeTemporalClient()
    msg = FakeMsg(_raw(_gap_envelope("42", "created")))
    # dispatch_message re-raises; the loop is what swallows + logs.
    with pytest.raises(RuntimeError, match="handler blew up"):
        asyncio.run(run.dispatch_message(d, msg, client))
    assert msg.acked is False  # un-acked -> JetStream redelivers
    assert msg.termed is False


# --- run.run wiring (end-to-end, fakes) -----------------------------------


def test_run_wires_subscriptions_and_dispatches():
    gap_d = gap_ready.DISPATCHERS[0]
    created = FakeMsg(_raw(_gap_envelope("42", "created")))
    sub = FakeSubscription([[created]])
    nats = FakeNatsClient({(gap_d.subject, gap_d.durable): sub})
    client = FakeTemporalClient()

    async def drive():
        stop = asyncio.Event()
        task = asyncio.create_task(
            run.run(
                dispatchers=[gap_d],
                nats_client=nats,
                temporal_client=client,
                stop=stop,
                # tiny poll timeout via the loop default is fine; FakeSubscription
                # raises TimeoutError after the first batch.
            )
        )
        # Let the loop fetch the batch + dispatch, then re-poll (idle) at least
        # once before stopping.
        for _ in range(5):
            await asyncio.sleep(0)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(drive())

    assert (gap_d.subject, gap_d.durable) in nats.subscribed
    assert created.acked is True
    assert client.calls[0]["kwargs"]["id"] == "gap-drain-42"
    # Injected NATS client is NOT closed by run() (caller owns its lifecycle).
    assert nats.closed is False


def test_run_handler_error_does_not_kill_loop():
    gap_d = gap_ready.DISPATCHERS[0]
    # First message is malformed (termed), second is a valid created event that
    # must still be dispatched — proving one bad message doesn't kill the loop.
    bad = FakeMsg(b"garbage")
    good = FakeMsg(_raw(_gap_envelope("7", "created")))
    sub = FakeSubscription([[bad, good]])
    nats = FakeNatsClient({(gap_d.subject, gap_d.durable): sub})
    client = FakeTemporalClient()

    async def drive():
        stop = asyncio.Event()
        task = asyncio.create_task(
            run.run(
                dispatchers=[gap_d],
                nats_client=nats,
                temporal_client=client,
                stop=stop,
            )
        )
        for _ in range(5):
            await asyncio.sleep(0)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(drive())

    assert bad.termed is True
    assert good.acked is True
    assert client.calls[0]["kwargs"]["id"] == "gap-drain-7"


def test_run_no_dispatchers_is_a_noop():
    nats = FakeNatsClient({})
    client = FakeTemporalClient()
    # No dispatchers -> no subscriptions, returns immediately.
    asyncio.run(run.run(dispatchers=[], nats_client=nats, temporal_client=client))
    assert nats.subscribed == []
