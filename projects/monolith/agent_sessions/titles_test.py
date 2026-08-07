from __future__ import annotations

import asyncio

import pytest
from sqlmodel import Session, SQLModel, create_engine

from agent_sessions import titles
from agent_sessions.models import AgentSession, AgentTurn


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'titles_test.db'}",
        connect_args={"check_same_thread": False},
    )
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            yield session
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


def _session_with_turns(session: Session, name: str = "s1") -> AgentSession:
    row = AgentSession(local_session_id=name, workspace="<guest>", branch="main")
    session.add(row)
    session.commit()
    session.refresh(row)
    session.add_all(
        [
            AgentTurn(
                session_id=row.id,
                seq=1,
                prompt="Find events in Vancouver",
                result_text="a list of events",
            ),
            AgentTurn(
                session_id=row.id,
                seq=2,
                prompt="Now narrow to Saturday",
                result_text="narrowed",
                voice_summary="Narrowed the list to Saturday.",
            ),
        ]
    )
    session.commit()
    return row


def test_pick_stale_sessions_builds_candidate(session):
    row = _session_with_turns(session)

    candidates = titles.pick_stale_sessions(session)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["session_id"] == row.id
    assert candidate["turn_seq"] == 2
    assert candidate["first_prompt"] == "Find events in Vancouver"
    assert candidate["latest_prompt"] == "Now narrow to Saturday"
    # voice_summary preferred over raw result text.
    assert candidate["latest_summary"] == "Narrowed the list to Saturday."


def test_stored_title_marks_session_fresh_until_next_turn(session):
    row = _session_with_turns(session)

    titles.store_title(session, row.id, "Vancouver weekend events", 2)
    assert titles.pick_stale_sessions(session) == []

    session.refresh(row)
    assert row.title == "Vancouver weekend events"
    assert row.title_turn_seq == 2

    # A new turn makes the title stale again: refresh-on-activity.
    session.add(
        AgentTurn(session_id=row.id, seq=3, prompt="Add Sunday", result_text="ok")
    )
    session.commit()
    stale = titles.pick_stale_sessions(session)
    assert [c["session_id"] for c in stale] == [row.id]
    assert stale[0]["turn_seq"] == 3


def test_sessions_without_turns_are_not_candidates(session):
    session.add(
        AgentSession(local_session_id="empty", workspace="<guest>", branch="main")
    )
    session.commit()
    assert titles.pick_stale_sessions(session) == []


def test_sanitize_title():
    assert titles.sanitize_title('"Fix the CI  pipeline."') == "Fix the CI pipeline"
    assert titles.sanitize_title('"Fix it".') == "Fix it"
    assert titles.sanitize_title("Name:\nplan the demo") == "Name: plan the demo"
    assert titles.sanitize_title(None) == ""
    assert len(titles.sanitize_title("x" * 500)) == titles.TITLE_MAX_CHARS


def test_build_title_prompt_clips_and_includes_context():
    prompt = titles.build_title_prompt(
        {
            "first_prompt": "Find events",
            "latest_prompt": "Narrow it",
            "latest_summary": "y" * 1000,
        }
    )
    assert "Find events" in prompt
    assert "Narrow it" in prompt
    assert "y" * 300 in prompt
    assert "y" * 301 not in prompt


def test_refresh_titles_once_names_and_stores(monkeypatch):
    monkeypatch.setenv("LLAMA_CPP_URL", "http://qwen.test")
    stored = []
    monkeypatch.setattr(
        titles,
        "_pick_stale_sessions_sync",
        lambda limit: [
            {
                "session_id": 7,
                "turn_seq": 4,
                "first_prompt": "a",
                "latest_prompt": "b",
                "latest_summary": "c",
            }
        ],
    )
    monkeypatch.setattr(
        titles,
        "_store_title_sync",
        lambda session_id, title, turn_seq: stored.append(
            (session_id, title, turn_seq)
        ),
    )

    async def fake_llm(prompt):
        assert "a" in prompt and "b" in prompt
        return '"Demo session name."'

    named = asyncio.run(titles.refresh_titles_once(call_llm=fake_llm))
    assert named == 1
    assert stored == [(7, "Demo session name", 4)]


def test_refresh_titles_once_survives_llm_failure(monkeypatch):
    monkeypatch.setenv("LLAMA_CPP_URL", "http://qwen.test")
    monkeypatch.setattr(
        titles,
        "_pick_stale_sessions_sync",
        lambda limit: [
            {
                "session_id": 7,
                "turn_seq": 4,
                "first_prompt": "a",
                "latest_prompt": "b",
                "latest_summary": "c",
            }
        ],
    )
    stored = []
    monkeypatch.setattr(
        titles,
        "_store_title_sync",
        lambda *args: stored.append(args),
    )

    async def broken_llm(prompt):
        raise RuntimeError("qwen is down")

    named = asyncio.run(titles.refresh_titles_once(call_llm=broken_llm))
    assert named == 0
    assert stored == []


def test_refresh_titles_once_skips_without_llm_url(monkeypatch):
    monkeypatch.delenv("LLAMA_CPP_URL", raising=False)

    def explode(limit):
        raise AssertionError("should not query the database")

    monkeypatch.setattr(titles, "_pick_stale_sessions_sync", explode)
    assert asyncio.run(titles.refresh_titles_once()) == 0
