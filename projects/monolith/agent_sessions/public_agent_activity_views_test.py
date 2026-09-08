"""Real-Postgres contract tests for public agent activity views and grants."""

from __future__ import annotations

import json

import pytest
from sqlmodel import Session, create_engine, text

from app import jobs_main


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
        ("cache_write_tokens", "numeric"),
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

    local_columns = session.execute(
        text(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public_api'
              AND table_name = 'local_session_activity_daily'
            ORDER BY ordinal_position
            """
        )
    ).all()
    assert local_columns == [
        ("day", "date"),
        ("model", "text"),
        ("source", "text"),
        ("sessions", "bigint"),
        ("input_tokens", "numeric"),
        ("output_tokens", "numeric"),
        ("cache_read_tokens", "numeric"),
        ("list_cost_usd", "numeric"),
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
        '{"input_tokens":10.0,"output_tokens":5.0,"cache_read_tokens":3.0,"cache_write_tokens":4.0}',
        cost_usd=0.1,
        list_cost_usd=0.2,
    )
    _insert_turn(
        session,
        first,
        2,
        '{"input_tokens":2.0,"output_tokens":4.0,"cache_read_input_tokens":8.0,"cache_creation_input_tokens":7.0}',
        list_cost_usd=0.05,
    )
    _insert_turn(
        session,
        second,
        1,
        '{"input_tokens":5.0,"output_tokens":6.0,"cached_input_tokens":9.0,"cache_write_input_tokens":11.0}',
        cost_usd=0.3,
    )
    _insert_turn(
        session,
        unknown,
        1,
        '{"input_tokens":1.0,"output_tokens":2.0}',
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
                   cache_read_tokens, cache_write_tokens, cost_usd, list_cost_usd
            FROM public_api.agent_activity_daily
            ORDER BY model
            """
        )
    ).all()
    assert len(rows) == 2
    luna, unknown_row = rows
    assert tuple(luna[:7]) == ("luna", 2, 3, 17, 15, 20, 22)
    assert float(luna.cost_usd) == pytest.approx(0.4)
    assert float(luna.list_cost_usd) == pytest.approx(0.25)
    assert tuple(unknown_row) == ("unknown", 1, 1, 1, 2, 0, 0, None, None)


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
            session.execute(
                text(
                    "SELECT day, model, source "
                    "FROM public_api.local_session_activity_daily"
                )
            ).all()
    finally:
        engine.dispose()

    for query in (
        text("SELECT id FROM agent_sessions.agent_sessions"),
        text("SELECT id FROM agent_sessions.agent_turns"),
        text("SELECT id FROM knowledge.raw_inputs"),
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


def test_local_session_view_aggregates_collector_usage(session):
    view_day = session.execute(text("SELECT CURRENT_DATE")).scalar_one()
    session.execute(
        text(
            """
            INSERT INTO knowledge.raw_inputs
                (raw_id, path, source, content_hash, created_at, extra)
            VALUES
                ('local-claude-one', 'local-claude-one.md', 'claude-session',
                 'local-hash-one', CURRENT_DATE + TIME '01:00:00',
                 '{"started_at":"2026-09-07T01:00:00Z","model":"claude-opus-5","usage":{"input_tokens":"10","output_tokens":"5","cache_read_tokens":"3"},"usage_cost_usd":"0.25"}'::jsonb),
                ('local-claude-two', 'local-claude-two.md', 'claude-session',
                 'local-hash-two', CURRENT_DATE + TIME '03:00:00',
                 '{"started_at":"2026-09-07T03:00:00Z","model":"claude-opus-5","usage":{"input_tokens":"2","output_tokens":"4","cache_read_tokens":"8"},"usage_cost_usd":"0.05"}'::jsonb),
                ('local-codex', 'local-codex.md', 'codex-session',
                 'local-hash-three', CURRENT_DATE + TIME '05:00:00',
                 '{"started_at":"2026-09-07T05:00:00Z","usage":{"input_tokens":"7","output_tokens":"6","cache_read_tokens":"9"}}'::jsonb),
                ('local-ignored', 'local-ignored.md', 'capture',
                 'local-hash-four', CURRENT_DATE + TIME '05:00:00',
                 '{"started_at":"2026-09-07T05:00:00Z","model":"luna","usage":{"input_tokens":"99"}}'::jsonb),
                ('local-empty-start', 'local-empty-start.md', 'codex-session',
                 'local-hash-five', CURRENT_DATE + TIME '06:00:00',
                 '{"started_at":"","model":"empty-start","usage":{"input_tokens":"3"}}'::jsonb),
                ('local-bad-usage', 'local-bad-usage.md', 'codex-session',
                 'local-hash-six', CURRENT_DATE + TIME '07:00:00',
                 '{"model":"usage-string","usage":"none"}'::jsonb),
                ('local-bad-token', 'local-bad-token.md', 'codex-session',
                 'local-hash-seven', CURRENT_DATE + TIME '08:00:00',
                 '{"model":"bad-input","usage":{"input_tokens":"abc","output_tokens":"2"},"usage_cost_usd":"not-a-price"}'::jsonb)
            """
        )
    )

    rows = session.execute(
        text(
            """
            SELECT day, model, source, sessions, input_tokens, output_tokens,
                   cache_read_tokens, list_cost_usd
            FROM public_api.local_session_activity_daily
            WHERE day = CURRENT_DATE
            ORDER BY model
            """
        )
    ).all()

    assert len(rows) == 4
    bad_input, claude, empty_start, unknown = rows
    assert tuple(bad_input) == (
        view_day,
        "bad-input",
        "codex-session",
        1,
        None,
        2,
        None,
        None,
    )
    assert tuple(claude[:7]) == (
        view_day,
        "claude-opus-5",
        "claude-session",
        2,
        12,
        9,
        11,
    )
    assert float(claude.list_cost_usd) == pytest.approx(0.30)
    assert tuple(unknown) == (
        view_day,
        "unknown",
        "codex-session",
        1,
        7,
        6,
        9,
        None,
    )
    assert tuple(empty_start) == (
        view_day,
        "empty-start",
        "codex-session",
        1,
        3,
        None,
        None,
        None,
    )


def test_raw_pricing_backfill_recovers_after_failed_jsonb_merge(pg):
    engine = create_engine(pg.url)
    failed_id = "pricing-backfill-failed"
    priced_id = "pricing-backfill-priced"
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "DROP TRIGGER IF EXISTS raw_pricing_backfill_reject_test "
                    "ON knowledge.raw_inputs"
                )
            )
            connection.execute(
                text("DROP FUNCTION IF EXISTS raw_pricing_backfill_reject_test()")
            )
            connection.execute(
                text(
                    "DELETE FROM knowledge.raw_inputs "
                    "WHERE raw_id IN (:failed_id, :priced_id)"
                ),
                {"failed_id": failed_id, "priced_id": priced_id},
            )
            connection.execute(
                text(
                    """
                    CREATE FUNCTION raw_pricing_backfill_reject_test()
                    RETURNS trigger AS $$
                    BEGIN
                      IF NEW.raw_id = 'pricing-backfill-failed' THEN
                        RAISE EXCEPTION 'forced pricing update failure';
                      END IF;
                      RETURN NEW;
                    END;
                    $$ LANGUAGE plpgsql
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TRIGGER raw_pricing_backfill_reject_test
                    BEFORE UPDATE ON knowledge.raw_inputs
                    FOR EACH ROW
                    EXECUTE FUNCTION raw_pricing_backfill_reject_test()
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO knowledge.raw_inputs
                        (raw_id, path, source, content_hash, created_at, extra)
                    VALUES
                        (:failed_id, 'pricing-backfill-failed.md',
                         'codex-session', 'pricing-backfill-failed-hash', now(),
                         CAST(:failed_extra AS jsonb)),
                        (:priced_id, 'pricing-backfill-priced.md',
                         'codex-session', 'pricing-backfill-priced-hash', now(),
                         CAST(:priced_extra AS jsonb))
                    """
                ),
                {
                    "failed_id": failed_id,
                    "priced_id": priced_id,
                    "failed_extra": json.dumps(
                        {
                            "model": "luna",
                            "usage": {"shape": "codex", "input_tokens": 1000},
                        }
                    ),
                    "priced_extra": json.dumps(
                        {
                            "model": "luna",
                            "usage": {"shape": "codex", "input_tokens": 1000},
                            "extraction_status": "complete",
                            "extraction_version": "v2",
                            "collector_version": "codex-v1",
                            "other": {"nested": True},
                        }
                    ),
                },
            )

        report = jobs_main._price_raws_backfill_core(engine, chunk_size=2)

        assert report.priced == 1
        with Session(engine) as session:
            rows = {
                row.raw_id: row.extra
                for row in session.execute(
                    text(
                        "SELECT raw_id, extra FROM knowledge.raw_inputs "
                        "WHERE raw_id IN (:failed_id, :priced_id)"
                    ),
                    {"failed_id": failed_id, "priced_id": priced_id},
                ).all()
            }
        assert "usage_cost_usd" not in rows[failed_id]
        assert rows[priced_id]["usage_cost_usd"] > 0
        assert rows[priced_id]["usage_cost_source"] == "list"
        assert rows[priced_id]["extraction_status"] == "complete"
        assert rows[priced_id]["extraction_version"] == "v2"
        assert rows[priced_id]["collector_version"] == "codex-v1"
        assert rows[priced_id]["other"] == {"nested": True}
    finally:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "DROP TRIGGER IF EXISTS raw_pricing_backfill_reject_test "
                    "ON knowledge.raw_inputs"
                )
            )
            connection.execute(
                text("DROP FUNCTION IF EXISTS raw_pricing_backfill_reject_test()")
            )
            connection.execute(
                text(
                    "DELETE FROM knowledge.raw_inputs "
                    "WHERE raw_id IN (:failed_id, :priced_id)"
                ),
                {"failed_id": failed_id, "priced_id": priced_id},
            )
        engine.dispose()
