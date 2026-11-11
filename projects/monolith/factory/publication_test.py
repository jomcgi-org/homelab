"""Unit tests for the public factory snapshot shaping.

Every helper under test takes plain dicts, so these run without a database and
without linking the swarm package, the same split private_view_test.py uses.
"""

from __future__ import annotations

import zlib

from factory.publication import (
    DEFAULT_MAX_REVIEW_ROUNDS,
    DIFF_LIMIT,
    decode_diff,
    latest_attempt_finish,
    review_rounds,
    session_payload,
    shape_activities,
    shape_brief,
    shape_node,
    shape_permission_denials,
    shape_policy,
    shape_pr,
    shape_rationale,
    shape_stop_events,
    shape_usage,
    shape_work_item,
    task_payload,
    task_phase,
    task_summary,
    turn_digest,
    turn_record,
)


def test_public_snapshot_never_loads_or_serializes_private_intake_backlog(monkeypatch):
    from dataclasses import asdict
    import json

    from factory import private_view, publication

    calls = []

    def private_board(*, session, plan_tasks, include_backlog):
        calls.append((session, plan_tasks, include_backlog))
        assert include_backlog is False
        # Even a reader returning an unexpected private field must not leak it.
        return {
            "ok": True,
            "state": "enabled",
            "intake_backlog": {"secret_issue": "private-backlog-sentinel"},
        }

    monkeypatch.setattr(private_view, "build_factory_view", private_board)
    monkeypatch.setattr(publication, "_public_work_items", lambda *args: {})
    monkeypatch.setattr(publication, "_board_task_ids", lambda *args: ([], {}))
    monkeypatch.setattr(publication, "_raw_policy", lambda *args: {})
    monkeypatch.setattr(publication, "_sessions_and_turns", lambda *args: ({}, {}))
    monkeypatch.setattr(publication, "_task_list_costs", lambda *args: {})
    monkeypatch.setattr(publication, "ledger_totals", lambda *args: {})
    db = object()
    result = publication.build_public_snapshot(db)
    assert calls == [(db, [], False)]
    serialized = json.dumps(asdict(result))
    assert "intake_backlog" not in serialized
    assert "private-backlog-sentinel" not in serialized


def test_public_work_item_exposes_local_record_without_private_factory_details():
    item = {
        "id": 100123,
        "title": "<script>untrusted title</script>",
        "body": "sensitive task detail that must remain private",
        "state": "ready",
        "task_class": "bug-fix",
        "labels": ["agent-ready", "<img onerror=alert(1)>"],
        "trust": "trusted",
        "authority": "github",
        "github_repo": "owner/repo",
        "github_issue_number": 6257,
        "source_ref": "javascript:alert(1)",
        "created_at": "2026-09-20T10:00:00Z",
        "updated_at": "2026-09-20T11:00:00Z",
        "receipts": [{"escalation_json": "private"}],
        "events": [{"author": "operator@example.test"}],
        "credential": "secret",
    }
    payload = shape_work_item(
        item,
        [{"to_id": 100124, "kind": "blocks", "source": "github_body", "id": 1}],
        [{"from_id": 100125, "kind": "parent", "source": "decision", "id": 2}],
        "2026-09-20T12:00:00+00:00",
    )

    assert payload["item"]["title"] == "<script>untrusted title</script>"
    assert payload["item"]["labels"][-1] == "<img onerror=alert(1)>"
    assert payload["item"]["source_ref"] == "https://github.com/owner/repo/issues/6257"
    assert payload["edges_out"] == [
        {"to_id": 100124, "kind": "blocks", "source": "github_body"}
    ]
    encoded = str(payload)
    assert "escalation_json" not in encoded
    assert "operator@example.test" not in encoded
    assert "credential" not in encoded
    assert "sensitive task detail" not in encoded


def _attempt(attempt, status, session_id, created_at, finished_at=None):
    return {
        "attempt": attempt,
        "status": status,
        "cost_usd": 0.5,
        "session_id": session_id,
        "created_at": created_at,
        "finished_at": finished_at,
    }


def _node(node_key, state, attempts=(), **extra):
    return {
        "node_key": node_key,
        "label": node_key.replace("_", " "),
        "kind": "work",
        "model": "sol",
        "deps": [],
        "state": state,
        "attempts": list(attempts),
        **extra,
    }


def _receipt(**extra):
    receipt = {
        "id": 7,
        "issue_number": 6014,
        "generation": 3,
        "title": "Publish the factory board",
        "url": "https://github.com/jomcgi/homelab/issues/6014",
        "state": "admitted",
        "task_id": "task-abc",
        "task_class": "delivery",
        "admitted_at": "2026-09-11T09:00:00+00:00",
        "deadline_at": "2026-09-11T13:00:00+00:00",
        "turns_used": 9,
        "planner_turns_used": 2,
        "committed_cost_usd": 1.25,
        "allowance": {"turns": 40},
        "evidence": None,
        "nodes": [],
        "stop_events": [],
    }
    receipt.update(extra)
    return receipt


def _turn(**extra):
    turn = {
        "seq": 3,
        "prompt": "implement the snapshot job",
        "result_text": "done\n\nRATIONALE\n- path: a.py · why: the writer",
        "terminal_reason": "complete",
        "stop_reason": "end_turn",
        "permission_denials": "[]",
        "commit_sha": "cafe1234",
        "base_sha": "beef5678",
        "diff_blob": None,
        "diff_truncated": False,
        "diff_base_sha": None,
        "usage_json": '{"input_tokens": 120, "output_tokens": 30, '
        '"activities": [{"type": "edit", "file_path": "a.py"}]}',
        "cost_usd": 0.3,
        "list_cost_usd": 0.25,
        "created_at": "2026-09-11T09:05:00+00:00",
    }
    turn.update(extra)
    return turn


def test_policy_carries_the_board_block_and_the_review_cap():
    board = {
        "generation": 4,
        "max_tasks": 2,
        "conductor_model": "opus",
        "worker_model": "sol",
        "reviewer_model": "opus",
        "max_task_turns_hard": 60,
        "max_parallel_nodes": 3,
        "task_budget_usd": 12.0,
        "max_attempts": 2,
    }
    shaped = shape_policy(board, {"max_review_rounds": 5, "actor": "joe"})
    assert shaped == {**board, "max_review_rounds": 5}
    assert "actor" not in shaped

    # A policy written before the cap existed gets the engine's own default.
    assert shape_policy(board, {})["max_review_rounds"] == DEFAULT_MAX_REVIEW_ROUNDS
    # An explicit zero is a real value, not a missing one.
    assert shape_policy(board, {"max_review_rounds": 0})["max_review_rounds"] == 0
    assert shape_policy(None, None)["conductor_model"] is None


def test_summary_uses_the_board_word_and_never_carries_identity():
    receipt = _receipt(
        state="succeeded",
        evidence={
            "pr_url": "https://github.com/jomcgi/homelab/pull/6020",
            "state": "merged",
            "reason": "approved",
            "review_session_id": 44,
        },
        nodes=[
            _node(
                "implement_fix",
                "done",
                [
                    _attempt(
                        1,
                        "succeeded",
                        10,
                        "2026-09-11T09:01:00+00:00",
                        "2026-09-11T09:30:00+00:00",
                    )
                ],
            ),
            _node(
                "correct_1",
                "done",
                [
                    _attempt(
                        1,
                        "succeeded",
                        11,
                        "2026-09-11T09:40:00+00:00",
                        "2026-09-11T09:55:00+00:00",
                    )
                ],
            ),
        ],
        actor="joe@example.com",
        triggered_by="joe@example.com",
    )

    summary = task_summary(receipt)

    assert summary["state"] == "landed"
    assert summary["issue_number"] == 6014
    assert summary["review_rounds"] == 1
    assert summary["allowance_turns"] == 40
    assert summary["committed_cost_usd"] == 1.25
    assert summary["finished_at"] == "2026-09-11T09:55:00+00:00"
    assert summary["pr"] == {
        "number": 6020,
        "url": "https://github.com/jomcgi/homelab/pull/6020",
        "state": "merged",
    }
    assert summary["nodes"] == [
        {"node_key": "implement_fix", "state": "done"},
        {"node_key": "correct_1", "state": "done"},
    ]
    assert "actor" not in summary
    assert "triggered_by" not in summary
    # review_session_id is an internal handle; only the URL and state go out.
    assert set(summary["pr"]) == {"number", "url", "state"}


def test_summary_has_no_pr_and_no_finish_while_in_flight():
    summary = task_summary(
        _receipt(
            nodes=[
                _node(
                    "implement_fix",
                    "running",
                    [_attempt(1, "admitted", 10, "2026-09-11T09:01:00+00:00")],
                )
            ]
        )
    )
    assert summary["state"] == "uncertain"
    assert summary["pr"] is None
    assert summary["finished_at"] is None


def test_phase_names_the_running_node_then_the_last_one_to_start():
    running = _receipt(
        nodes=[
            _node("implement_fix", "done", [_attempt(1, "succeeded", 1, "A")]),
            _node("review_fix", "running", [_attempt(1, "admitted", 2, "B")]),
        ]
    )
    assert task_phase(running) == "review_fix"

    finished = _receipt(
        state="succeeded",
        nodes=[
            _node("implement_fix", "done", [_attempt(1, "succeeded", 1, "A")]),
            _node("review_fix", "done", [_attempt(1, "succeeded", 2, "B")]),
            _node("correct_1", "retired", []),
        ],
    )
    assert task_phase(finished) == "review_fix"


def test_phase_falls_back_to_a_word_when_no_node_ever_ran():
    assert task_phase(_receipt(state="queued", nodes=[])) == "queued"
    assert task_phase(_receipt(state="admitted", nodes=[])) == "queued"
    assert task_phase(_receipt(state="succeeded", nodes=[])) == "done"
    assert task_phase(_receipt(state="cancelled", nodes=[])) == "cancelled"
    # A task that stopped without a node getting anywhere was escalated out.
    assert task_phase(_receipt(state="uncertain", nodes=[])) == "escalated"
    assert task_phase(_receipt(state="failed", nodes=[])) == "escalated"


def test_retired_and_pending_node_states_survive_to_the_public_node():
    nodes = [
        _node("implement_fix", "done", [_attempt(1, "succeeded", 1, "A", "B")]),
        _node("review_fix", "pending", []),
        _node("correct_1", "retired", []),
        _node("correct_2", "failed", [_attempt(1, "failed", 2, "C", "D")]),
    ]
    assert [node["state"] for node in nodes] == [
        "done",
        "pending",
        "retired",
        "failed",
    ]
    assert review_rounds(nodes) == 2
    assert latest_attempt_finish(nodes) == "D"
    assert latest_attempt_finish([_node("x", "pending", [])]) is None


def test_node_joins_attempts_to_their_session_key_and_turn_digests():
    node = _node(
        "implement_fix",
        "done",
        [
            _attempt(
                1,
                "failed",
                10,
                "2026-09-11T09:01:00+00:00",
                "2026-09-11T09:10:00+00:00",
            ),
            _attempt(
                2,
                "succeeded",
                11,
                "2026-09-11T09:11:00+00:00",
                "2026-09-11T09:20:00+00:00",
            ),
        ],
    )
    shaped = shape_node(
        node,
        {
            10: "factory:task-abc:implement_fix:1",
            11: "factory:task-abc:implement_fix:2",
        },
        {10: [_turn(seq=1)], 11: [_turn(seq=1), _turn(seq=2)]},
    )

    assert shaped["node_key"] == "implement_fix"
    assert [attempt["session_key"] for attempt in shaped["attempts"]] == [
        "factory:task-abc:implement_fix:1",
        "factory:task-abc:implement_fix:2",
    ]
    assert [len(attempt["turns"]) for attempt in shaped["attempts"]] == [1, 2]
    digest = shaped["attempts"][0]["turns"][0]
    assert set(digest) == {
        "seq",
        "prompt",
        "activities",
        "result_text",
        "cost_usd",
        "commit_sha",
    }
    # A digest is deliberately diff-free: the diff lives on the session page.
    assert "diff" not in digest


def test_node_attempt_with_no_session_yet_has_a_null_key_and_no_turns():
    shaped = shape_node(
        _node("plan", "running", [_attempt(1, "admitted", None, "A")]), {}, {}
    )
    assert shaped["attempts"][0]["session_key"] is None
    assert shaped["attempts"][0]["turns"] == []


def test_diff_decompresses_and_flags_its_own_truncation():
    body = "diff --git a/a.py b/a.py\n+one line\n"
    diff, truncated = decode_diff(zlib.compress(body.encode()), False)
    assert diff == body and truncated is False

    # The stored flag alone is enough to mark a short diff truncated.
    _, stored = decode_diff(zlib.compress(body.encode()), True)
    assert stored is True

    big = zlib.compress(b"x" * (DIFF_LIMIT + 10))
    diff, truncated = decode_diff(big, False)
    assert len(diff) == DIFF_LIMIT and truncated is True

    assert decode_diff(None, False) == (None, False)
    assert decode_diff(b"not-zlib", False) == (None, True)


def test_turn_record_carries_the_diff_rationale_and_usage():
    record = turn_record(_turn(diff_blob=zlib.compress(b"diff --git a/a.py b/a.py\n")))

    assert record["diff"] == "diff --git a/a.py b/a.py\n"
    assert record["diff_truncated"] is False
    assert record["rationale"] == {
        "raw": "RATIONALE\n- path: a.py · why: the writer",
        "parse_status": "parsed",
    }
    # paths and deviations stay private; only the raw trailer and status ship.
    assert set(record["rationale"]) == {"raw", "parse_status"}
    assert record["usage"] == {
        "input_tokens": 120,
        "output_tokens": 30,
        "cache_read_tokens": None,
    }
    assert record["activities"] == [{"type": "edit", "file_path": "a.py"}]
    assert record["permission_denials"] == []
    assert record["base_sha"] == "beef5678"


def test_rationale_is_null_when_the_result_has_no_trailer():
    assert shape_rationale("just a result") is None
    assert shape_rationale(None) is None
    unparseable = shape_rationale("RATIONALE\nnot a bullet at all")
    assert unparseable["parse_status"] == "unparseable"


def test_usage_folds_the_cache_read_aliases_and_is_null_when_empty():
    assert shape_usage({"cache_read_input_tokens": 40})["cache_read_tokens"] == 40
    assert shape_usage({"cached_input_tokens": 7})["cache_read_tokens"] == 7
    assert shape_usage({"activities": []}) is None
    assert shape_usage(None) is None


def test_activities_keep_only_the_shim_keys_and_the_last_three_hundred():
    usage = {
        "activities": [
            {"type": "bash", "command": "ci", "secret": "nope"},
            {"type": "tool_use", "name": "Grep"},
            "not-a-dict",
        ]
    }
    assert shape_activities(usage) == [
        {"type": "bash", "command": "ci"},
        {"type": "tool_use", "name": "Grep"},
    ]

    many = {
        "activities": [{"type": "edit", "file_path": f"{i}.py"} for i in range(400)]
    }
    shaped = shape_activities(many)
    assert len(shaped) == 300 and shaped[0]["file_path"] == "100.py"
    assert shape_activities({"activities": "nope"}) == []


def test_permission_denials_parse_from_their_stored_json_string():
    assert shape_permission_denials('["Bash(rm)"]') == ["Bash(rm)"]
    assert shape_permission_denials(None) == []
    assert shape_permission_denials("not json") == []
    assert shape_permission_denials('{"a": 1}') == []


def test_brief_splits_the_issue_body_on_blank_lines_and_bounds_it():
    body = "first\npara\r\n\n  \n second para \n\n" + "\n\n".join(
        f"para {i}" for i in range(10)
    )
    brief = shape_brief(body)
    assert brief[0] == "first\npara"
    assert brief[1] == "second para"
    assert len(brief) == 6
    assert shape_brief(None) == [] and shape_brief("") == []


def test_stop_events_drop_the_actor_and_the_request_key():
    shaped = shape_stop_events(
        [
            {
                "action": "stop_request",
                "actor": "joe@example.com",
                "request_key": "abc",
                "reason": "operator stop",
                "intervention_required": True,
                "created_at": "2026-09-11T10:00:00+00:00",
                "session_id": 12,
            }
        ]
    )
    assert shaped == [
        {
            "action": "stop_request",
            "reason": "operator stop",
            "intervention_required": True,
            "at": "2026-09-11T10:00:00+00:00",
        }
    ]
    assert shape_stop_events(None) == []


def test_pr_is_null_without_evidence_and_tolerates_an_odd_url():
    assert shape_pr(None) is None
    assert shape_pr({"reason": "no pr"}) is None
    assert shape_pr({"pr_url": "https://example.test/pulls/"})["number"] is None


def test_task_payload_adds_the_brief_plan_and_stop_events_to_the_summary():
    receipt = _receipt(
        nodes=[
            _node(
                "implement_fix",
                "running",
                [_attempt(1, "admitted", 10, "2026-09-11T09:01:00+00:00")],
            )
        ],
        stop_events=[
            {
                "action": "stop_intent",
                "actor": "joe@example.com",
                "reason": "budget",
                "intervention_required": False,
                "created_at": "2026-09-11T09:02:00+00:00",
            }
        ],
        evidence={"reason": "muse could not reach the catalog"},
    )
    payload = task_payload(
        receipt,
        shape_policy({"conductor_model": "opus"}, {}),
        "the brief\n\nsecond paragraph",
        {10: "factory:task-abc:implement_fix:1"},
        {10: [_turn()]},
        "2026-09-11T10:00:00+00:00",
    )

    assert payload["snapshotted_at"] == "2026-09-11T10:00:00+00:00"
    assert payload["policy"]["conductor_model"] == "opus"
    assert payload["task"]["brief"] == ["the brief", "second paragraph"]
    assert payload["task"]["evidence_reason"] == "muse could not reach the catalog"
    assert payload["task"]["nodes"][0]["attempts"][0]["turns"][0]["seq"] == 3
    assert payload["task"]["stop_events"][0]["action"] == "stop_intent"
    assert "actor" not in payload["task"]["stop_events"][0]


def test_digest_tolerates_a_turn_with_no_usable_usage_json():
    digest = turn_digest(_turn(usage_json="not json", list_cost_usd=None))
    assert digest["activities"] == []
    assert digest["cost_usd"] is None


def test_session_payload_totals_the_turns_and_excludes_session_identity():
    payload = session_payload(
        {
            "local_session_id": "factory:task-abc:implement_fix:2",
            "model": "sol",
            "status": "finished",
            "ember_session_id": "guest-9",
            "created_at": "2026-09-11T09:11:00+00:00",
            "last_turn_at": "2026-09-11T09:20:00+00:00",
            "title": "a generated name",
            "triggered_by": "joe@example.com",
            "voice_summary": "spoken",
        },
        6014,
        "implement_fix",
        2,
        # Reported costs are ignored: the public pages show list price.
        [
            _turn(seq=1, cost_usd=9.0, list_cost_usd=0.25),
            _turn(seq=2, cost_usd=9.0, list_cost_usd=0.75),
        ],
        "2026-09-11T10:00:00+00:00",
    )

    assert payload["session"] == {
        "key": "factory:task-abc:implement_fix:2",
        "issue_number": 6014,
        "node_key": "implement_fix",
        "attempt": 2,
        "model": "sol",
        "status": "finished",
        "guest_bound": True,
        "created_at": "2026-09-11T09:11:00+00:00",
        "last_turn_at": "2026-09-11T09:20:00+00:00",
        "terminal_reason": "complete",
        "turn_count": 2,
        "cost_usd": 1.0,
    }
    assert [turn["seq"] for turn in payload["turns"]] == [1, 2]
    assert "title" not in payload["session"]
    assert "triggered_by" not in payload["session"]
    assert "voice_summary" not in payload["session"]
    assert "ember_session_id" not in payload["session"]


def test_session_payload_has_a_null_cost_when_no_turn_could_be_priced():
    payload = session_payload(
        {"local_session_id": "factory:t:n:1", "ember_session_id": None},
        None,
        "n",
        1,
        [_turn(cost_usd=None, list_cost_usd=None)],
        "2026-09-11T10:00:00+00:00",
    )
    assert payload["session"]["cost_usd"] is None
    assert payload["session"]["guest_bound"] is False


def test_max_tasks_collapses_the_per_lane_dict_to_the_delivery_count():
    from factory.publication import flatten_max_tasks, shape_policy

    assert flatten_max_tasks({"delivery": 2, "advisory": 0}) == 2
    assert flatten_max_tasks(1) == 1
    assert flatten_max_tasks(None) is None
    assert flatten_max_tasks(True) is None
    shaped = shape_policy({"max_tasks": {"delivery": 3, "advisory": 1}}, None)
    assert shaped["max_tasks"] == 3


def test_payload_encoding_keeps_non_ascii_as_utf8():
    from factory.publication import _encode

    assert "\\u00b7" not in _encode({"label": "implement \u00b7 fix"})
    assert "\u00b7" in _encode({"label": "implement \u00b7 fix"})


def test_turn_list_cost_reads_tokenless_turns_as_free_and_unknown_as_null():
    from factory.publication import turn_list_cost

    assert turn_list_cost(_turn(list_cost_usd=0.4)) == 0.4
    # A turn whose session create failed recorded no tokens and cost nothing.
    assert (
        turn_list_cost(
            _turn(list_cost_usd=None, usage_json='{"activities": [], "recovery": {}}')
        )
        == 0.0
    )
    # Tokens on a model the price table does not know stay unknown.
    assert turn_list_cost(_turn(list_cost_usd=None)) is None
    assert turn_list_cost(_turn(list_cost_usd=None, usage_json=None)) is None


def test_attempt_and_task_costs_are_list_price_not_the_budget_ledger():
    node = _node(
        "implement_fix",
        "done",
        [
            _attempt(1, "failed", 10, "2026-09-11T09:01:00+00:00"),
            _attempt(2, "succeeded", 11, "2026-09-11T09:10:00+00:00"),
            _attempt(3, "reserved", None, "2026-09-11T09:20:00+00:00"),
        ],
    )
    shaped = shape_node(
        node,
        {10: "k1", 11: "k2"},
        {
            10: [_turn(list_cost_usd=0.5), _turn(seq=4, list_cost_usd=0.25)],
            11: [_turn(list_cost_usd=None, usage_json='{"activities": []}')],
        },
    )
    # The settlement figure (_attempt's 0.5) is not what the page shows.
    assert [attempt["cost_usd"] for attempt in shaped["attempts"]] == [
        0.75,
        0.0,
        None,
    ]

    summary = task_summary(_receipt(), 3.5)
    assert summary["cost_usd"] == 3.5
    assert summary["committed_cost_usd"] == 1.25
    assert task_summary(_receipt())["cost_usd"] is None


def _ember(day, model, *, sessions=1, list_cost=None, unpriced=0):
    return {
        "day": day,
        "model": model,
        "sessions": sessions,
        "turns": sessions * 2,
        "input_tokens": 10,
        "output_tokens": 1,
        "cache_read_tokens": 5,
        "list_cost_usd": list_cost,
        "unpriced_turns": unpriced,
        # A private column a future query might return must not leak.
        "session_id": 99,
    }


def _local(day, model, *, list_cost=None, unpriced=0):
    return {
        "day": day,
        "model": model,
        "source": "codex-session",
        "sessions": 1,
        "input_tokens": 100,
        "output_tokens": 10,
        "cache_read_tokens": 50,
        "list_cost_usd": list_cost,
        "unpriced_sessions": unpriced,
    }


def test_agent_activity_sums_ember_and_local_at_list_price_over_both_windows():
    from datetime import date, datetime, timedelta

    from factory.publication import shape_agent_activity

    today = date(2026, 10, 1)

    def ago(days):
        return today - timedelta(days=days)

    payload = shape_agent_activity(
        {
            "active_last_hour": 3,
            "sessions_today": 5,
            "running": 2,
            "last_turn_at": datetime(2026, 10, 1, 11, 30),
        },
        [
            _ember(today, "opus", list_cost=2.0),
            _ember(today, "astra", list_cost=1.0, unpriced=1),
            # An outage day: turns that never reached a model cost 0.0.
            _ember(ago(5), "astra", sessions=280, list_cost=0.0),
            _ember(ago(10), "sol", list_cost=4.0),
            # Only unpriced spend: the day is unknown, not free.
            _ember(ago(12), "mystery", list_cost=None, unpriced=3),
            _ember(ago(30), "sol", list_cost=100.0),
        ],
        [
            _local(today, "gpt-6-astra", list_cost=10.0),
            _local(ago(8), "claude-opus-5-5", list_cost=20.0),
            _local(ago(29), "gpt-6-astra", list_cost=0.5),
        ],
        today=today,
        snapshotted_at="2026-10-01T12:00:00+00:00",
    )

    assert payload["cost_basis"] == "list"
    assert payload["now"]["last_turn_at"] == "2026-10-01T11:30:00+00:00"
    assert "session_id" not in payload["daily"][0]
    assert ago(30).isoformat() not in {row["day"] for row in payload["daily"]}

    spend = {row["day"]: row["spend_usd"] for row in payload["spend_daily"]}
    assert len(payload["spend_daily"]) == 30
    assert spend[today.isoformat()] == 13.0
    assert spend[ago(5).isoformat()] == 0.0
    assert spend[ago(1).isoformat()] == 0.0
    assert spend[ago(12).isoformat()] is None
    assert spend[ago(8).isoformat()] == 20.0

    week = payload["totals_7d"]
    assert week["ember"]["list_cost_usd"] == 3.0
    assert week["local"]["list_cost_usd"] == 10.0
    assert week["combined"]["spend_usd"] == 13.0
    assert week["combined"]["unpriced"] == 1
    assert week["combined"]["sessions"] == 1 + 1 + 280 + 1

    month = payload["totals_30d"]["combined"]
    assert month["spend_usd"] == 2.0 + 1.0 + 4.0 + 10.0 + 20.0 + 0.5
    assert month["unpriced"] == 4


def _quota_fixture():
    from datetime import datetime, timezone

    now = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)

    def account(used, reset, age=60):
        return {
            "provider": "codex",
            "observed": True,
            "age_seconds": age,
            "account_id": "never-public",
            "windows": [
                {
                    "name": "primary",
                    "used_percent": used,
                    "window_minutes": 10080,
                    "resets_at": reset,
                }
            ],
        }

    return now, {
        "available": True,
        "fetched_at": now.isoformat(),
        "grants_valid": True,
        "grants_complete": True,
        "grants": {
            "secret-account-a": account(20, "2026-10-21T00:00:00Z"),
            "secret-account-b": account(40, "2026-10-22T18:00:00Z"),
        },
        "providers": {"codex": account(99, "2026-10-21T00:00:00Z")},
    }


def test_quota_equal_account_mean_uses_actual_duration_and_staggered_resets():
    from factory.publication import shape_quota

    now, raw = _quota_fixture()
    result = shape_quota(raw, now)
    codex = result["providers"][0]
    assert codex["configured_accounts"] == 2
    window = codex["windows"][0]
    assert window["window_seconds"] == 604800  # primary is weekly here
    assert window["used_percent"] == 30  # provider's 99 is not a third account
    assert window["elapsed_percent"] == 37.5
    assert window["remaining_seconds"] == 378000
    assert window["pace_delta_percent"] == -7.5
    assert window["next_reset_at"] == "2026-10-21T00:00:00+00:00"
    assert window["last_reset_at"] == "2026-10-22T18:00:00+00:00"
    assert window["complete"] is True
    assert "secret-account" not in str(result)
    assert "never-public" not in str(result)
    assert "account_id" not in str(result)


def test_quota_provider_fallback_has_unknown_coverage_and_no_total_pace():
    from factory.publication import shape_quota

    now, raw = _quota_fixture()
    raw["providers"]["claude"] = {**raw["providers"]["codex"], "provider": "claude"}
    claude = shape_quota(raw, now)["providers"][1]
    assert claude["source"] == "provider"
    assert claude["configured_accounts"] is None
    assert claude["inventory_complete"] is False
    assert claude["windows"][0]["used_percent"] == 99
    assert claude["windows"][0]["pace_delta_percent"] is None


def test_quota_unobserved_account_never_becomes_zero_usage_or_complete_coverage():
    from factory.publication import shape_quota

    now, raw = _quota_fixture()
    raw["grants"]["secret-account-b"] = {"provider": "codex", "observed": False}
    window = shape_quota(raw, now)["providers"][0]["windows"][0]
    assert window["used_percent"] == 20
    assert window["reported_accounts"] == 1
    assert window["complete"] is False
    assert window["pace_delta_percent"] is None


def test_quota_stale_report_is_visible_without_fresh_pace():
    from factory.publication import shape_quota

    now, raw = _quota_fixture()
    raw["grants"]["secret-account-a"]["age_seconds"] = 10000
    window = shape_quota(raw, now)["providers"][0]["windows"][0]
    assert window["used_percent"] == 30
    assert window["fresh_accounts"] == 1
    assert window["pace_delta_percent"] is None


def test_quota_expired_invalid_duplicate_and_unknown_windows_are_not_averaged():
    from factory.publication import shape_quota

    invalid = [float("nan"), float("inf"), -1, 101, True, "20"]
    for value in invalid:
        now, raw = _quota_fixture()
        raw["grants"]["secret-account-a"]["windows"][0]["used_percent"] = value
        window = shape_quota(raw, now)["providers"][0]["windows"][0]
        assert window["used_percent"] == 40
        assert window["reported_accounts"] == 1
        assert window["pace_delta_percent"] is None
    for reset in (
        "2026-10-17T12:00:00Z",
        "2026-10-17T11:00:00Z",
        "invalid",
        None,
        "2027-01-01T00:00:00Z",
    ):
        now, raw = _quota_fixture()
        raw["grants"]["secret-account-a"]["windows"][0]["resets_at"] = reset
        window = shape_quota(raw, now)["providers"][0]["windows"][0]
        assert window["reported_accounts"] == 1
    now, raw = _quota_fixture()
    first = raw["grants"]["secret-account-a"]["windows"]
    first.append(dict(first[0]))
    raw["grants"]["secret-account-b"]["windows"].append(
        {"name": "secondary", "used_percent": 0}
    )
    codex = shape_quota(raw, now)["providers"][0]
    assert codex["windows"][0]["reported_accounts"] == 1
    assert codex["unknown_windows"] == 1
    assert len(codex["windows"]) == 1


def test_quota_separates_short_and_weekly_limits_and_gates_unknown_inventory():
    from factory.publication import shape_quota

    now, raw = _quota_fixture()
    for account in raw["grants"].values():
        account["windows"].append(
            {
                "name": "secondary",
                "window_minutes": 300,
                "used_percent": 10,
                "resets_at": "2026-10-17T14:30:00Z",
            }
        )
    raw["grants_complete"] = False
    codex = shape_quota(raw, now)["providers"][0]
    assert [window["window_seconds"] for window in codex["windows"]] == [18000, 604800]
    assert all(window["pace_delta_percent"] is None for window in codex["windows"])
    assert codex["windows"][0]["used_percent"] == 10
    assert codex["windows"][0]["elapsed_percent"] == 50


def test_quota_failure_and_all_missing_values_have_no_zero_mean():
    from factory.publication import shape_quota

    now, raw = _quota_fixture()
    for account in raw["grants"].values():
        account["windows"][0]["expired"] = True
    window = shape_quota(raw, now)["providers"][0]["windows"][0]
    assert window["used_percent"] is None
    assert window["reported_accounts"] == 0
    raw["available"] = False
    result = shape_quota(raw, now)
    assert result["available"] is False
    assert all(provider["windows"] == [] for provider in result["providers"])
    assert shape_quota(None, now)["available"] is False


def test_quota_history_starts_at_the_first_real_observation_and_deduplicates_cache():
    from datetime import timedelta

    from factory.publication import shape_quota, shape_quota_history

    now, raw = _quota_fixture()
    history = shape_quota_history(shape_quota(raw, now), None)
    points = history[0]["points"]
    assert len(points) == 1
    assert points[0]["sampled_at"] == now.isoformat()
    assert points[0]["used_percent"] == 30
    assert points[0]["elapsed_percent"] == 37.5
    assert points[0]["status"] == "complete"
    assert (
        shape_quota_history(shape_quota(raw, now + timedelta(minutes=5)), history)
        == history
    )


def test_quota_history_stale_and_unavailable_samples_are_gaps_until_recovery():
    from datetime import timedelta

    from factory.publication import shape_quota, shape_quota_history

    now, raw = _quota_fixture()
    history = shape_quota_history(shape_quota(raw, now), None)
    history = shape_quota_history(
        shape_quota(raw, now + timedelta(minutes=20)), history
    )
    stale = history[0]["points"][-1]
    assert stale["status"] == "stale"
    assert stale["used_percent"] is None
    assert stale["elapsed_percent"] is None
    assert (
        shape_quota_history(shape_quota(raw, now + timedelta(minutes=25)), history)
        == history
    )
    history = shape_quota_history(
        shape_quota(None, now + timedelta(minutes=30)), history
    )
    assert history[0]["points"][-1]["status"] == "unavailable"
    raw["fetched_at"] = (now + timedelta(minutes=35)).isoformat()
    history = shape_quota_history(
        shape_quota(raw, now + timedelta(minutes=35)), history
    )
    recovered = history[0]["points"][-1]
    assert recovered["used_percent"] == 30
    assert recovered["break_before"] is True
    assert recovered["reset_before"] is False


def test_quota_history_partial_account_coverage_is_a_gap_but_provider_observation_is_usable():
    from datetime import timedelta

    from factory.publication import shape_quota, shape_quota_history

    now, raw = _quota_fixture()
    history = shape_quota_history(shape_quota(raw, now), None)
    raw["grants"]["secret-account-b"] = {"provider": "codex", "observed": False}
    raw["providers"]["claude"] = {**raw["providers"]["codex"], "provider": "claude"}
    history = shape_quota_history(shape_quota(raw, now + timedelta(minutes=2)), history)
    claude, codex = history
    assert claude["points"][0]["status"] == "observed"
    assert claude["points"][0]["used_percent"] == 99
    assert claude["points"][0]["complete"] is False
    assert claude["points"][0]["configured_accounts"] is None
    assert codex["points"][-1]["status"] == "partial"
    assert codex["points"][-1]["used_percent"] is None
    assert codex["points"][-1]["elapsed_percent"] is None
    assert codex["points"][-1]["break_before"] is True


def test_quota_history_losing_earliest_reset_account_breaks_without_claiming_reset():
    from datetime import timedelta

    from factory.publication import shape_quota, shape_quota_history

    for removed in (False, True):
        now, raw = _quota_fixture()
        history = shape_quota_history(shape_quota(raw, now), None)
        if removed:
            del raw["grants"]["secret-account-a"]
        else:
            raw["grants"]["secret-account-a"] = {
                "provider": "codex",
                "observed": False,
            }
        history = shape_quota_history(
            shape_quota(raw, now + timedelta(minutes=2)), history
        )
        point = history[0]["points"][-1]
        assert point["next_reset_at"] == "2026-10-22T18:00:00+00:00"
        assert point["break_before"] is True
        assert point["reset_before"] is False


def test_quota_history_reset_clock_jitter_preserves_continuous_usage():
    from datetime import datetime, timedelta

    from factory.publication import shape_quota, shape_quota_history

    for account in ("secret-account-a", "secret-account-b"):
        now, raw = _quota_fixture()
        history = shape_quota_history(shape_quota(raw, now), None)
        window = raw["grants"][account]["windows"][0]
        reset = datetime.fromisoformat(window["resets_at"])
        for minute, jitter in ((2, 1), (4, 0)):
            window["resets_at"] = (reset + timedelta(seconds=jitter)).isoformat()
            window["used_percent"] += 1
            raw["fetched_at"] = (now + timedelta(minutes=minute)).isoformat()
            history = shape_quota_history(
                shape_quota(raw, now + timedelta(minutes=minute)), history
            )
            point = history[0]["points"][-1]
            assert point["used_percent"] == 30 + minute / 4
            assert point["break_before"] is False
            assert point["reset_before"] is False


def test_quota_history_reset_clock_jitter_tolerance_is_bounded():
    from datetime import datetime, timedelta

    from factory.publication import shape_quota, shape_quota_history

    for seconds in (-61, -60, 60, 61):
        now, raw = _quota_fixture()
        history = shape_quota_history(shape_quota(raw, now), None)
        window = raw["grants"]["secret-account-a"]["windows"][0]
        window["resets_at"] = (
            datetime.fromisoformat(window["resets_at"]) + timedelta(seconds=seconds)
        ).isoformat()
        window["used_percent"] += 1
        raw["fetched_at"] = (now + timedelta(minutes=2)).isoformat()
        history = shape_quota_history(
            shape_quota(raw, now + timedelta(minutes=2)), history
        )
        point = history[0]["points"][-1]
        assert point["break_before"] is (abs(seconds) > 60)
        assert point["reset_before"] is (seconds > 60)


def test_quota_history_short_window_rollover_is_not_reset_clock_jitter():
    from datetime import timedelta

    from factory.publication import shape_quota, shape_quota_history

    now, raw = _quota_fixture()
    for account in raw["grants"].values():
        window = account["windows"][0]
        window["window_minutes"] = 1
        window["used_percent"] = 80
        window["resets_at"] = (now + timedelta(seconds=30)).isoformat()
    history = shape_quota_history(shape_quota(raw, now), None)
    raw["fetched_at"] = (now + timedelta(seconds=40)).isoformat()
    for account in raw["grants"].values():
        window = account["windows"][0]
        window["used_percent"] = 20
        window["resets_at"] = (now + timedelta(seconds=90)).isoformat()
    history = shape_quota_history(
        shape_quota(raw, now + timedelta(seconds=40)), history
    )
    point = history[0]["points"][-1]
    assert point["break_before"] is True
    assert point["reset_before"] is True


def test_quota_history_reset_and_usage_correction_break_the_line_without_false_reset_labels():
    from datetime import timedelta

    from factory.publication import shape_quota, shape_quota_history

    now, raw = _quota_fixture()
    initial = shape_quota_history(shape_quota(raw, now), None)
    first_window = raw["grants"]["secret-account-a"]["windows"][0]
    first_window["used_percent"] = 10
    # Give the correction a fresh observation so it is a measured decrease.
    raw["fetched_at"] = (now + timedelta(minutes=20)).isoformat()
    corrected = shape_quota_history(
        shape_quota(raw, now + timedelta(minutes=20)), initial
    )
    assert corrected[0]["points"][-1]["break_before"] is True
    assert corrected[0]["points"][-1]["reset_before"] is False
    first_window["resets_at"] = "2026-10-23T00:00:00Z"
    reset = shape_quota_history(
        shape_quota(raw, now + timedelta(minutes=21)), corrected
    )
    assert reset[0]["points"][-1]["break_before"] is True
    assert reset[0]["points"][-1]["reset_before"] is True


def test_quota_history_retains_first_latest_and_breaks_within_fifteen_minute_buckets():
    from datetime import timedelta

    from factory.publication import shape_quota, shape_quota_history

    now, raw = _quota_fixture()
    history = shape_quota_history(shape_quota(raw, now), None)
    history = shape_quota_history(
        shape_quota(None, now + timedelta(minutes=2)), history
    )
    for minute in (4, 8, 12):
        raw["fetched_at"] = (now + timedelta(minutes=minute)).isoformat()
        raw["grants"]["secret-account-a"]["windows"][0]["used_percent"] = 20 + minute
        history = shape_quota_history(
            shape_quota(raw, now + timedelta(minutes=minute)), history
        )
    points = history[0]["points"]
    assert len(points) == 2
    assert points[0]["sampled_at"] == now.isoformat()
    assert points[-1]["sampled_at"] == (now + timedelta(minutes=12)).isoformat()
    assert points[-1]["used_percent"] == 36
    assert points[-1]["break_before"] is True


def test_quota_history_bounds_retention_and_revalidates_every_stored_public_field():
    import json
    from datetime import timedelta

    from factory.publication import shape_quota, shape_quota_history

    now, raw = _quota_fixture()
    original = shape_quota_history(shape_quota(raw, now), None)[0]
    template = original["points"][0]
    rows = []
    for minute in range(0, 9 * 24 * 60, 5):
        sampled = now - timedelta(minutes=minute)
        rows.append(
            {
                **template,
                "sampled_at": sampled.isoformat(),
                "observed_at": sampled.isoformat(),
                "next_reset_at": (sampled + timedelta(days=1)).isoformat(),
                "last_reset_at": (sampled + timedelta(days=2)).isoformat(),
                "account_id": "private-identity",
                "credential": "private-token",
            }
        )
    rows.extend(
        [
            None,
            {**template, "status": {"private-identity": True}},
            {**template, "sampled_at": "private-identity"},
        ]
    )
    previous = [
        {**original, "points": rows, "account_id": "private-identity"},
        {"provider": "private-identity", "window_seconds": 1, "points": rows},
    ]
    result = shape_quota_history(shape_quota(None, now), previous)
    points = result[0]["points"]
    assert len(points) <= 2 * (8 * 24 * 4 + 1)
    assert points[0]["sampled_at"] >= (now - timedelta(days=8)).isoformat()
    assert points[-1]["sampled_at"] == now.isoformat()
    encoded = json.dumps(result, allow_nan=False)
    assert "private-identity" not in encoded
    assert "private-token" not in encoded
    assert "account_id" not in encoded
    assert "credential" not in encoded


def test_quota_history_rejects_nonfinite_values_and_forged_complete_coverage():
    from datetime import timedelta

    from factory.publication import shape_quota, shape_quota_history

    now, raw = _quota_fixture()
    for bad in (float("nan"), float("inf"), 10**400, True, "secret"):
        history = shape_quota_history(shape_quota(raw, now), None)
        history[0]["points"][0]["used_percent"] = bad
        result = shape_quota_history(
            shape_quota(None, now + timedelta(minutes=1)), history
        )
        assert result[0]["points"][0]["used_percent"] is None
    history = shape_quota_history(shape_quota(raw, now), None)
    history[0]["points"][0]["reported_accounts"] = 1
    result = shape_quota_history(shape_quota(None, now + timedelta(minutes=1)), history)
    assert result[0]["points"][0]["complete"] is False
    assert result[0]["points"][0]["used_percent"] is None


def _quota_with_pacing():
    from datetime import datetime, timedelta

    now, raw = _quota_fixture()
    for offset, view in enumerate(raw["grants"].values(), start=1):
        budget = view["windows"][0]
        reset = datetime.fromisoformat(budget["resets_at"])
        remaining = (reset - now).total_seconds()
        target = 100 * (1 - remaining / 604800)
        delta = budget["used_percent"] - target
        view["windows"].append(
            {
                "window_minutes": 300,
                "used_percent": 90,
                "resets_at": (now + timedelta(hours=offset)).isoformat(),
            }
        )
        view["pacing"] = {
            "state": "conserve"
            if delta > 5
            else "use_more"
            if delta < -5
            else "on_pace",
            "evaluated_at": now.isoformat(),
            "budget_window_minutes": 10080,
            "budget_used_percent": budget["used_percent"],
            "budget_remaining_percent": 100 - budget["used_percent"],
            "budget_resets_at": budget["resets_at"],
            "budget_remaining_seconds": remaining,
            "target_used_percent": target,
            "pace_delta_percent": delta,
            "budget_per_day_percent": (100 - budget["used_percent"])
            * 86400
            / remaining,
            "short_window_minutes": 300,
            "short_resets_at": view["windows"][1]["resets_at"],
            "private_detail": "never-public",
        }
    return now, raw


def test_quota_pacing_aggregates_allowlisted_means_and_reset_ranges():
    from factory.publication import shape_quota

    now, raw = _quota_with_pacing()
    pacing = shape_quota(raw, now)["providers"][0]["pacing"]
    assert pacing["complete"] is True
    assert pacing["state_counts"] == {"use_more": 1, "conserve": 1}
    assert pacing["budget_window_seconds"] == 604800
    assert pacing["budget_remaining_percent"] == 70
    assert pacing["budget_per_day_percent"] == (80 / 3.5 + 60 / 5.25) / 2
    assert pacing["short_next_reset_at"] == "2026-10-17T13:00:00+00:00"
    assert pacing["short_last_reset_at"] == "2026-10-17T14:00:00+00:00"
    assert pacing["valid_until"] == "2026-10-17T13:00:00+00:00"
    assert "never-public" not in str(pacing)
    assert "secret-account" not in str(pacing)


def test_quota_pacing_keeps_mixed_blocked_accounts_visible_without_all_blocked_claim():
    from factory.publication import shape_quota

    now, raw = _quota_with_pacing()
    first = next(iter(raw["grants"].values()))
    first["pacing"]["state"] = "wait_for_reset"
    first["windows"][1]["used_percent"] = 100
    first["pacing"]["blocked_until"] = first["windows"][1]["resets_at"]
    pacing = shape_quota(raw, now)["providers"][0]["pacing"]
    assert pacing["state_counts"] == {"wait_for_reset": 1, "conserve": 1}
    assert pacing["blocked_next_reset_at"] == "2026-10-17T13:00:00+00:00"
    assert pacing["blocked_last_reset_at"] == "2026-10-17T13:00:00+00:00"
    assert pacing["reported_accounts"] == 2
    assert pacing["complete"] is True


def test_quota_pacing_does_not_publish_daily_budget_for_partial_or_different_budgets():
    from factory.publication import shape_quota

    for mutation in ("stale", "missing", "inventory", "different_duration"):
        now, raw = _quota_with_pacing()
        first = next(iter(raw["grants"].values()))
        if mutation == "stale":
            first["age_seconds"] = 901
        elif mutation == "missing":
            first.pop("pacing")
        elif mutation == "inventory":
            raw["grants_complete"] = False
        else:
            first["windows"][0]["window_minutes"] = 20160
            first["pacing"]["budget_window_minutes"] = 20160
        pacing = shape_quota(raw, now)["providers"][0]["pacing"]
        assert pacing["complete"] is False
        assert "budget_per_day_percent" not in pacing
        assert "short_next_reset_at" not in pacing


def test_quota_pacing_rejects_malformed_stale_or_inconsistent_broker_metadata():
    from factory.publication import shape_quota

    for key, bad in (
        ("budget_per_day_percent", float("nan")),
        ("budget_used_percent", 101),
        ("budget_remaining_percent", True),
        ("target_used_percent", "50"),
        ("pace_delta_percent", float("inf")),
        ("budget_remaining_seconds", 100),
        ("budget_resets_at", "2027-01-01T00:00:00Z"),
        ("evaluated_at", "2026-10-17T12:00:01Z"),
        ("evaluated_at", "2026-10-17T11:44:00Z"),
        ("state", "private_detail"),
    ):
        now, raw = _quota_with_pacing()
        first = next(iter(raw["grants"].values()))
        first["pacing"][key] = bad
        pacing = shape_quota(raw, now)["providers"][0]["pacing"]
        assert pacing["complete"] is False, (key, bad)
        assert "budget_per_day_percent" not in pacing


def test_quota_pacing_provider_fallback_keeps_reported_scope_and_old_broker_works():
    from factory.publication import shape_quota

    now, raw = _quota_with_pacing()
    raw["providers"]["claude"] = {
        **next(iter(raw["grants"].values())),
        "provider": "claude",
    }
    provider = shape_quota(raw, now)["providers"][1]
    assert provider["source"] == "provider"
    assert provider["configured_accounts"] is None
    assert provider["pacing"]["complete"] is True
    assert provider["pacing"]["reported_accounts"] == 1
    now, raw = _quota_fixture()
    assert shape_quota(raw, now)["providers"][0].get("pacing") is None


def test_quota_pacing_malformed_view_does_not_break_other_publication():
    from factory.publication import shape_quota

    for key, value in (("age_seconds", 1e300), ("age_seconds", -1), ("windows", 4)):
        now, raw = _quota_with_pacing()
        first = next(iter(raw["grants"].values()))
        first[key] = value
        result = shape_quota(raw, now)
        assert result["available"] is True
        assert result["providers"][0]["pacing"]["complete"] is False


def test_quota_pacing_does_not_roll_a_reported_short_reset_into_a_future_cycle():
    from datetime import timedelta

    from factory.publication import shape_quota

    now, raw = _quota_with_pacing()
    for view in raw["grants"].values():
        view["windows"][1]["resets_at"] = (now + timedelta(minutes=1)).isoformat()
        view["pacing"]["short_resets_at"] = view["windows"][1]["resets_at"]
    pacing = shape_quota(raw, now + timedelta(minutes=2))["providers"][0]["pacing"]
    assert pacing["valid_until"] == "2026-10-17T12:01:00+00:00"
    assert "short_next_reset_at" not in pacing


def test_quota_pacing_rejects_a_short_reset_not_matching_provider_evidence():
    from factory.publication import shape_quota

    for invalid in (
        True,
        None,
        "invalid",
        "2026-10-17T14:00:00Z",
        "2027-01-01T00:00:00Z",
    ):
        now, raw = _quota_with_pacing()
        next(iter(raw["grants"].values()))["pacing"]["short_resets_at"] = invalid
        pacing = shape_quota(raw, now)["providers"][0]["pacing"]
        assert pacing["complete"] is True
        assert "short_next_reset_at" not in pacing


def test_quota_pacing_does_not_invent_a_binding_reset_for_a_limited_account():
    from factory.publication import shape_quota

    now, raw = _quota_with_pacing()
    first = next(iter(raw["grants"].values()))
    first["pacing"]["state"] = "wait_for_reset"
    first["windows"][1]["used_percent"] = 100
    first["pacing"]["blocked_until"] = first["windows"][1]["resets_at"]
    first["windows"].append({"used_percent": 100})
    pacing = shape_quota(raw, now)["providers"][0]["pacing"]
    assert pacing["state_counts"]["wait_for_reset"] == 1
    assert "blocked_next_reset_at" not in pacing


def test_activity_distinguishes_execution_pause_and_uncertainty_without_private_details():
    from factory.publication import activity_state

    running = {
        "state": "running",
        "node_key": "review_1",
        "session": {"status": "running", "guest_bound": True, "executing": True},
    }
    assert activity_state({"state": "admitted", "nodes": [running]}) == "executing"
    assert (
        activity_state({"state": "admitted", "task_paused": True, "nodes": [running]})
        == "executing"
    )
    assert activity_state({"state": "admitted", "task_paused": True}) == "paused"
    assert activity_state({"state": "admitted"}) == "waiting"
    assert (
        activity_state(
            {"state": "admitted", "task_paused": True, "unresolved_starts": 1}
        )
        == "uncertain"
    )
    assert activity_state({"state": "uncertain", "nodes": [running]}) == "uncertain"
    assert activity_state({"state": "escalated", "nodes": [running]}) == "escalated"
    summary = task_summary(
        {
            "state": "admitted",
            "task_paused": True,
            "escalation": {"reason": "private sentinel"},
            "nodes": [],
        }
    )
    assert summary["state"] == "paused"
    assert "private sentinel" not in str(summary)
    assert "task_paused" not in summary
