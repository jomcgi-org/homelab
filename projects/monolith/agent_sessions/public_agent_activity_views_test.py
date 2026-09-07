"""Real-Postgres contract tests for public agent activity views and grants."""

from __future__ import annotations

import pytest
from sqlmodel import Session, create_engine, text


def test_agent_activity_view_columns_and_types(session):
    daily_columns = session.execute(
        text(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public_api'
              AND table_name = 'agent_activity_daily'
            ORDER BY ordinal_position
            """
        )
    ).all()
    assert daily_columns == [
        ("day", "date"),
        ("model", "text"),
        ("sessions", "bigint"),
        ("turns", "bigint"),
        ("input_tokens", "numeric"),
        ("output_tokens", "numeric"),
        ("cache_read_tokens", "numeric"),
        ("cost_usd", "numeric"),
        ("list_cost_usd", "double precision"),
    ]

    now_columns = session.execute(
        text(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public_api'
              AND table_name = 'agent_activity_now'
            ORDER BY ordinal_position
            """
        )
    ).all()
    assert now_columns == [
        ("active_last_hour", "bigint"),
        ("sessions_today", "bigint"),
        ("last_turn_at", "timestamp with time zone"),
        ("running", "bigint"),
    ]


def _insert_session(session, local_id, model):
    return session.execute(
        text(
            """
            INSERT INTO agent_sessions.agent_sessions
                (local_session_id, workspace, branch, status, model,
                 created_at, last_turn_at)
            VALUES
                (:local_id, '/tmp/activity', 'feat/activity', 'running', :model,
                 now(), now())
            RETURNING id
            """
        ),
        {"local_id": local_id, "model": model},
    ).scalar_one()


def _insert_turn(
    session,
    session_id,
    seq,
    usage_json,
    *,
    cost_usd=None,
    list_cost_usd=None,
    age="1 day",
):
    session.execute(
        text(
            """
            INSERT INTO agent_sessions.agent_turns
                (session_id, seq, prompt, result_text, usage_json, cost_usd,
                 list_cost_usd, created_at)
            VALUES
                (:session_id, :seq, 'private prompt', 'private result',
                 :usage_json, :cost_usd, :list_cost_usd,
                 now() - CAST(:age AS interval))
            """
        ),
        {
            "session_id": session_id,
            "seq": seq,
            "usage_json": usage_json,
            "cost_usd": cost_usd,
            "list_cost_usd": list_cost_usd,
            "age": age,
        },
    )


def test_daily_view_aggregates_json_and_guards_empty_values(session):
    first = _insert_session(session, "activity-first", "luna")
    second = _insert_session(session, "activity-second", "luna")
    unknown = _insert_session(session, "activity-unknown", None)
    invalid = _insert_session(session, "activity-invalid", "sol")
    old = _insert_session(session, "activity-old", "terra")

    _insert_turn(
        session,
        first,
        1,
        '{"input_tokens":10,"output_tokens":5,"cache_read_tokens":3}',
        cost_usd=0.1,
        list_cost_usd=0.2,
    )
    _insert_turn(
        session,
        first,
        2,
        '{"input_tokens":2,"output_tokens":4,"cache_read_tokens":8}',
        list_cost_usd=0.05,
    )
    _insert_turn(
        session,
        second,
        1,
        '{"input_tokens":5,"output_tokens":6,"cache_read_tokens":9}',
        cost_usd=0.3,
    )
    _insert_turn(
        session,
        unknown,
        1,
        '{"input_tokens":1,"output_tokens":2,"cache_read_tokens":3}',
    )
    _insert_turn(session, invalid, 1, None)
    _insert_turn(session, invalid, 2, "")
    _insert_turn(
        session,
        old,
        1,
        '{"input_tokens":99,"output_tokens":99,"cache_read_tokens":99}',
        age="91 days",
    )

    rows = session.execute(
        text(
            """
            SELECT model, sessions, turns, input_tokens, output_tokens,
                   cache_read_tokens, cost_usd, list_cost_usd
            FROM public_api.agent_activity_daily
            ORDER BY model
            """
        )
    ).all()
    assert len(rows) == 2
    luna, unknown_row = rows
    assert tuple(luna[:6]) == ("luna", 2, 3, 17, 15, 20)
    assert float(luna.cost_usd) == pytest.approx(0.4)
    assert float(luna.list_cost_usd) == pytest.approx(0.25)
    assert tuple(unknown_row) == ("unknown", 1, 1, 1, 2, 3, None, None)


def test_public_reader_can_select_views_but_not_agent_tables(pg):
    engine = create_engine(pg.url)
    try:
        with Session(engine) as session:
            session.execute(text("SET ROLE public_reader"))
            session.execute(
                text("SELECT active_last_hour FROM public_api.agent_activity_now")
            ).all()
            session.execute(
                text("SELECT day, model FROM public_api.agent_activity_daily")
            ).all()
    finally:
        engine.dispose()

    for query in (
        text("SELECT id FROM agent_sessions.agent_sessions"),
        text("SELECT id FROM agent_sessions.agent_turns"),
    ):
        engine = create_engine(pg.url)
        try:
            with Session(engine) as session:
                session.execute(text("SET ROLE public_reader"))
                with pytest.raises(Exception) as exc:
                    session.execute(query).all()
                assert "permission denied" in str(exc.value).lower()
        finally:
            engine.dispose()
