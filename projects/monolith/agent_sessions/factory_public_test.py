"""Unit tests for the public factory snapshot shaping.

Every helper under test takes plain dicts, so these run without a database and
without linking the swarm package, the same split factory_view_test.py uses.
"""

from __future__ import annotations

import zlib

from agent_sessions.factory_public import (
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
    task_payload,
    task_phase,
    task_summary,
    turn_digest,
    turn_record,
)


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
        "cost_usd": 0.25,
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
    assert summary["state"] == "in flight"
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
    digest = turn_digest(_turn(usage_json="not json", cost_usd=None))
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
        [_turn(seq=1, cost_usd=0.25), _turn(seq=2, cost_usd=0.75)],
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


def test_session_payload_has_a_null_cost_when_no_turn_recorded_one():
    payload = session_payload(
        {"local_session_id": "factory:t:n:1", "ember_session_id": None},
        None,
        "n",
        1,
        [_turn(cost_usd=None)],
        "2026-09-11T10:00:00+00:00",
    )
    assert payload["session"]["cost_usd"] is None
    assert payload["session"]["guest_bound"] is False


def test_max_tasks_collapses_the_per_lane_dict_to_the_delivery_count():
    from agent_sessions.factory_public import flatten_max_tasks, shape_policy

    assert flatten_max_tasks({"delivery": 2, "advisory": 0}) == 2
    assert flatten_max_tasks(1) == 1
    assert flatten_max_tasks(None) is None
    assert flatten_max_tasks(True) is None
    shaped = shape_policy({"max_tasks": {"delivery": 3, "advisory": 1}}, None)
    assert shaped["max_tasks"] == 3


def test_payload_encoding_keeps_non_ascii_as_utf8():
    from agent_sessions.factory_public import _encode

    assert "\\u00b7" not in _encode({"label": "implement \u00b7 fix"})
    assert "\u00b7" in _encode({"label": "implement \u00b7 fix"})
