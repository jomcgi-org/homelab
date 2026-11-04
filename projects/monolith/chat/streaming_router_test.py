"""Unbuffered response timing and ownership checks, without model/cluster I/O."""

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import literal, select
from sqlmodel import Session, create_engine
from starlette.requests import ClientDisconnect

import chat.router as routes
from chat import explorer

pytestmark = pytest.mark.asyncio
TIMEOUT = 3


async def bounded(awaitable):
    return await asyncio.wait_for(awaitable, TIMEOUT)


def parse(frame):
    if isinstance(frame, bytes):
        frame = frame.decode()
    assert frame.startswith("data: ") and frame.endswith("\n\n")
    return json.loads(frame[6:-2])


async def drain(iterator):
    async def collect():
        return [parse(frame) async for frame in iterator]

    return await bounded(collect())


class ControlledAgent:
    def __init__(self, endpoint, failure=None, tool=None):
        self.endpoint = endpoint
        self.failure = failure
        self.tool = tool
        self.entered = asyncio.Event()
        self.text_started = asyncio.Event()
        self.stopped = asyncio.Event()
        self.cleanup_started = asyncio.Event()
        self.cleanup_gate = asyncio.Event()
        self.cleanup_gate.set()
        self.entry_gate = asyncio.Event()
        self.text_gate = asyncio.Event()
        self.finish_gate = asyncio.Event()
        self.completed = False
        self.task = None
        self.deps = None
        self.usage = SimpleNamespace(input_tokens=2, output_tokens=3, total_tokens=5)

    @asynccontextmanager
    async def run_stream(self, message, *, message_history, deps):
        self.task = asyncio.current_task()
        self.deps = deps
        self.message = message
        self.history = message_history
        try:
            if self.failure == "entry":
                raise RuntimeError("entry failure")
            if self.tool:
                await self.tool(deps)
            else:
                kind = "node_discovered" if self.endpoint == "explore" else "tool_call"
                deps.emitter.emit(kind, {"request": message})
            self.entered.set()
            await self.entry_gate.wait()
            yield self
            if self.failure == "exit":
                raise RuntimeError("exit failure")
            self.completed = True
        finally:
            self.cleanup_started.set()
            await self.cleanup_gate.wait()
            self.stopped.set()

    async def stream_text(self, *, delta):
        assert delta is True
        yield "first "
        self.text_started.set()
        await self.text_gate.wait()
        if self.failure == "partial":
            raise RuntimeError("partial failure")
        yield "second"
        await self.finish_gate.wait()

    def release(self):
        self.entry_gate.set()
        self.text_gate.set()
        self.finish_gate.set()
        self.cleanup_gate.set()


@pytest.fixture(params=["explore", "cluster"])
def endpoint(request):
    return request.param


@pytest.fixture
def make_response(monkeypatch, endpoint):
    usage = Mock()
    monkeypatch.setattr(routes.shared.inference, "record_usage", usage)
    monkeypatch.setattr(routes, "EmbeddingClient", lambda: AsyncMock())

    async def make(*, failure=None, tool=None, message="question", history=None):
        agent = ControlledAgent(endpoint, failure, tool)
        getter = "get_explorer_agent" if endpoint == "explore" else "get_cluster_agent"
        monkeypatch.setattr(routes, getter, lambda: agent)
        handler = routes.explore if endpoint == "explore" else routes.cluster_chat
        body_type = (
            routes.ExploreRequest
            if endpoint == "explore"
            else routes.ClusterChatRequest
        )
        response = await handler(
            body_type(message=message, history=history or []), Mock()
        )
        return response, agent, usage

    return make


async def next_while_blocked(iterator, barrier, agent, description):
    pending = asyncio.create_task(anext(iterator))
    try:
        await bounded(barrier.wait())
        done, _ = await asyncio.wait({pending}, timeout=TIMEOUT)
        assert pending in done, f"{description} was buffered until agent completion"
        assert not agent.completed
        return parse(pending.result())
    finally:
        if not pending.done():
            agent.release()
            await bounded(pending)


async def test_tool_and_text_arrive_before_agent_completion(make_response, endpoint):
    response, agent, usage = await make_response(
        history=[
            {"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": "earlier answer"},
        ]
    )
    iterator = response.body_iterator
    try:
        tool = await next_while_blocked(iterator, agent.entered, agent, "tool event")
        assert tool == {
            "type": "node_discovered" if endpoint == "explore" else "tool_call",
            "data": {"request": "question"},
        }
        assert not agent.entry_gate.is_set()
        agent.entry_gate.set()
        first = await next_while_blocked(
            iterator, agent.text_started, agent, "text delta"
        )
        assert first == {"type": "text_chunk", "data": {"text": "first "}}
        agent.text_gate.set()
        second = parse(await bounded(anext(iterator)))
        assert second == {"type": "text_chunk", "data": {"text": "second"}}
        assert not agent.completed
        agent.finish_gate.set()
        rest = await drain(iterator)
        assert rest == [{"type": "done", "data": {}}]
        assert agent.history[0].parts[0].content == "earlier question"
        assert agent.history[1].parts[0].content == "earlier answer"
        usage.assert_called_once_with(
            {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            routes.shared.inference.META_SPARK_MODEL,
            "explorer" if endpoint == "explore" else "cluster_agent",
        )
    finally:
        agent.release()
        await bounded(iterator.aclose())
    assert agent.task.done() and agent.stopped.is_set()


async def test_text_only_arrives_before_agent_completion(make_response):
    async def no_tools(deps):
        return None

    response, agent, _ = await make_response(tool=no_tools)
    agent.entry_gate.set()
    try:
        first = await next_while_blocked(
            response.body_iterator, agent.text_started, agent, "text delta"
        )
        assert first == {"type": "text_chunk", "data": {"text": "first "}}
        assert not agent.text_gate.is_set()
    finally:
        agent.release()
        await bounded(response.body_iterator.aclose())


@pytest.mark.parametrize("failure", ["entry", "partial", "exit"])
async def test_failure_has_exactly_one_terminal_outcome(make_response, failure):
    response, agent, usage = await make_response(failure=failure)
    agent.release()
    events = await drain(response.body_iterator)
    assert events[-1] == {"type": "error", "data": {"message": f"{failure} failure"}}
    assert [
        event["type"] for event in events if event["type"] in {"done", "error"}
    ] == ["error"]
    chunks = [
        event["data"]["text"] for event in events if event["type"] == "text_chunk"
    ]
    assert (
        chunks
        == {"entry": [], "partial": ["first "], "exit": ["first ", "second"]}[failure]
    )
    assert agent.task.done() and agent.stopped.is_set()
    assert agent.deps.emitter._closed
    if failure != "exit":
        usage.assert_not_called()


async def test_usage_failure_preserves_done(make_response):
    response, agent, usage = await make_response()
    usage.side_effect = RuntimeError("accounting unavailable")
    agent.release()
    events = await drain(response.body_iterator)
    assert events[-1] == {"type": "done", "data": {}}
    assert not any(event["type"] == "error" for event in events)
    assert agent.history is None


@pytest.mark.parametrize("stage", ["entry", "text"])
async def test_early_close_joins_producer(make_response, stage):
    response, agent, _ = await make_response()
    iterator = response.body_iterator
    await bounded(anext(iterator))
    if stage == "text":
        agent.entry_gate.set()
        assert parse(await bounded(anext(iterator)))["type"] == "text_chunk"
    await bounded(iterator.aclose())
    assert agent.stopped.is_set() and agent.task.done()
    assert agent.task.cancelled() and not agent.completed
    assert agent.deps.emitter._closed


async def test_cancel_consumer_propagates_and_joins_producer(make_response):
    response, agent, _ = await make_response()
    await bounded(anext(response.body_iterator))
    pending = asyncio.create_task(anext(response.body_iterator))
    # The queued callback runs after pending has reached the empty emitter queue.
    asyncio.get_running_loop().call_soon(pending.cancel)
    with pytest.raises(asyncio.CancelledError):
        await bounded(pending)
    assert agent.stopped.is_set() and agent.task.done()


@pytest.mark.parametrize("disconnect", ["receive", "send", "cancel_send"])
async def test_asgi_disconnect_closes_suspended_body(make_response, disconnect):
    response, agent, _ = await make_response()
    sent = asyncio.Event()
    bodies = []
    scope = {
        "type": "http",
        "asgi": {"spec_version": "2.3" if disconnect == "receive" else "2.4"},
    }

    async def receive():
        await sent.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.body" and message.get("body"):
            bodies.append(parse(message["body"]))
            assert not agent.completed
            sent.set()
            if disconnect == "send":
                raise OSError("connection lost")
            if disconnect == "cancel_send":
                raise asyncio.CancelledError

    try:
        if disconnect == "send":
            with pytest.raises(ClientDisconnect):
                await bounded(response(scope, receive, send))
        elif disconnect == "cancel_send":
            with pytest.raises(asyncio.CancelledError):
                await bounded(response(scope, receive, send))
        else:
            await bounded(response(scope, receive, send))
    finally:
        agent.release()
        await bounded(response.body_iterator.aclose())
    assert len(bodies) == 1
    assert agent.stopped.is_set() and agent.task.done()


async def test_requests_have_separate_producers_and_emitters(make_response):
    first, a, _ = await make_response(message="one")
    second, b, _ = await make_response(message="two")
    try:
        assert parse(await bounded(anext(first.body_iterator)))["data"] == {
            "request": "one"
        }
        assert parse(await bounded(anext(second.body_iterator)))["data"] == {
            "request": "two"
        }
        assert a.deps.emitter is not b.deps.emitter
        await bounded(first.body_iterator.aclose())
        assert a.task.done() and not b.task.done()
        b.release()
        remaining = await drain(second.body_iterator)
        assert remaining[-1] == {"type": "done", "data": {}}
    finally:
        a.release()
        b.release()
        await bounded(first.body_iterator.aclose())
        await bounded(second.body_iterator.aclose())


@pytest.fixture
def database(monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'explorer.db'}")
    sessions = []
    main_thread = threading.get_ident()

    class OwnedSession(Session):
        def __enter__(self):
            self.owner_thread = threading.get_ident()
            self.closed = False
            assert self.owner_thread != main_thread
            sessions.append(self)
            return super().__enter__()

        def close(self):
            assert threading.get_ident() == self.owner_thread
            super().close()
            self.closed = True

    def open_session(engine):
        # A closed session cannot silently reconnect and mask a lifetime bug.
        return OwnedSession(engine, close_resets_only=False)

    def check_session(session):
        assert not session.closed
        assert threading.get_ident() == session.owner_thread != main_thread
        assert session.exec(select(literal(42))).scalar_one() == 42

    def store(session):
        check_session(session)
        result = Mock()

        def search(**kwargs):
            check_session(session)
            return [
                {
                    "note_id": "note",
                    "title": "Note",
                    "type": "note",
                    "tags": [],
                    "snippet": "text",
                    "score": 1.0,
                    "observed_at": "2026-10-01T00:00:00Z",
                    "review_after": "2099-01-01T00:00:00Z",
                    "freshness": "current",
                }
            ]

        result.search_notes_with_context.side_effect = search
        result.get_note_by_id.side_effect = lambda note_id: search()[0]
        result.get_note_links.side_effect = lambda note_id: (
            check_session(session) or [{"target_id": "target"}]
        )
        return result

    monkeypatch.setattr(explorer, "get_engine", lambda: engine)
    monkeypatch.setattr(explorer, "Session", open_session)
    monkeypatch.setattr(explorer, "get_store", store)
    yield SimpleNamespace(
        sessions=sessions, engine=engine, check=check_session, store=store
    )
    assert all(session.closed for session in sessions)
    assert engine.pool.checkedout() == 0
    engine.dispose()


@pytest.mark.parametrize("endpoint", ["explore"])
@pytest.mark.parametrize("outcome", ["success", "entry", "partial", "close"])
async def test_explorer_uses_live_worker_sessions_and_releases_them(
    make_response, endpoint, database, outcome
):
    async def tool(deps):
        await explorer._search_kg(deps, "query")
        await explorer._expand_node(deps, "note")

    response, agent, _ = await make_response(tool=tool, failure=outcome)
    if outcome == "close":
        await bounded(anext(response.body_iterator))
        await bounded(agent.entered.wait())
        await bounded(response.body_iterator.aclose())
    else:
        agent.release()
        events = await drain(response.body_iterator)
        assert events[-1]["type"] == ("done" if outcome == "success" else "error")
    assert agent.task.done()
    assert len(database.sessions) == (0 if outcome == "entry" else 4)
    assert all(session.closed for session in database.sessions)


@pytest.mark.parametrize("endpoint", ["explore"])
async def test_explorer_cancellation_waits_for_inflight_session(
    make_response, endpoint, database, monkeypatch
):
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def store(session):
        result = database.store(session)

        def search(**kwargs):
            database.check(session)
            loop.call_soon_threadsafe(started.set)
            assert release.wait(TIMEOUT), "worker release was never signalled"
            database.check(session)
            return []

        result.search_notes_with_context.side_effect = search
        return result

    monkeypatch.setattr(explorer, "get_store", store)
    response, agent, _ = await make_response(
        tool=lambda deps: explorer._search_kg(deps, "query")
    )
    pending = asyncio.create_task(anext(response.body_iterator))
    try:
        await bounded(started.wait())
        assert len(database.sessions) == 1 and not database.sessions[0].closed
        pending.cancel()
        # Release from the event loop after cancellation has reached the join.
        loop.call_soon(release.set)
        with pytest.raises(asyncio.CancelledError):
            await bounded(pending)
        assert agent.task.done() and agent.stopped.is_set()
        assert database.sessions[0].closed
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await bounded(response.body_iterator.aclose())


async def test_unconsumed_response_has_no_producer_or_session(
    make_response, endpoint, database
):
    response, agent, _ = await make_response()
    await bounded(response.body_iterator.aclose())
    assert agent.task is None
    assert database.sessions == []


@pytest.mark.parametrize("endpoint", ["explore"])
@pytest.mark.parametrize("failure", ["store_setup", "store_read"])
async def test_store_failures_close_live_session(
    make_response, endpoint, database, monkeypatch, failure
):
    def store(session):
        database.check(session)
        if failure == "store_setup":
            raise RuntimeError(failure)
        result = database.store(session)
        result.search_notes_with_context.side_effect = RuntimeError(failure)
        return result

    monkeypatch.setattr(explorer, "get_store", store)
    response, agent, _ = await make_response(
        tool=lambda deps: explorer._search_kg(deps, "query")
    )
    assert await drain(response.body_iterator) == [
        {"type": "error", "data": {"message": failure}}
    ]
    assert agent.task.done()
    assert len(database.sessions) == 1 and database.sessions[0].closed


async def test_repeated_cancellation_still_awaits_producer_cleanup(make_response):
    response, agent, _ = await make_response()
    await bounded(anext(response.body_iterator))
    agent.cleanup_gate.clear()
    closing = asyncio.create_task(response.body_iterator.aclose())
    try:
        await bounded(agent.cleanup_started.wait())
        assert not closing.done() and not agent.task.done()
        closing.cancel()
        asyncio.get_running_loop().call_soon(closing.cancel)
        asyncio.get_running_loop().call_soon(agent.cleanup_gate.set)
        with pytest.raises(asyncio.CancelledError):
            await bounded(closing)
        assert agent.stopped.is_set() and agent.task.done()
    finally:
        agent.release()
        await bounded(asyncio.gather(closing, return_exceptions=True))
