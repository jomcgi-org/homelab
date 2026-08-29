from datetime import datetime, timedelta, timezone

from swarm import drain_console

NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=timezone.utc)


def _job(**overrides) -> dict:
    job = {
        "name": "qd-example",
        "routine_kind": "qwen-drain",
        "payload": {"prompt": "Audit the thing\nmore detail"},
        "next_run_at": None,
        "last_run_at": None,
        "last_status": None,
        "last_summary": None,
        "locked_by": None,
        "locked_at": None,
        "ttl_secs": 2100,
    }
    job.update(overrides)
    return job


class TestJobState:
    def test_live_lock_is_running(self):
        job = _job(
            locked_by="qwen-drainer",
            locked_at=NOW - timedelta(seconds=60),
            next_run_at=NOW - timedelta(hours=1),
        )
        assert drain_console.job_state(job, NOW) == "running"

    def test_expired_lock_falls_through_to_due(self):
        # The reclaimable case: the claim predicate ignores a dead lock, so
        # the console must not render it as running.
        job = _job(
            locked_by="qwen-drainer",
            locked_at=NOW - timedelta(seconds=3000),
            next_run_at=NOW - timedelta(hours=1),
        )
        assert drain_console.job_state(job, NOW) == "due"

    def test_future_next_run_is_scheduled(self):
        job = _job(next_run_at=NOW + timedelta(minutes=10))
        assert drain_console.job_state(job, NOW) == "scheduled"

    def test_completed_one_shot_states(self):
        ok = _job(last_run_at=NOW - timedelta(minutes=5), last_status="ok")
        err = _job(last_run_at=NOW - timedelta(minutes=5), last_status="error")
        assert drain_console.job_state(ok, NOW) == "ok"
        assert drain_console.job_state(err, NOW) == "error"

    def test_never_run_never_armed_is_parked(self):
        assert drain_console.job_state(_job(), NOW) == "parked"


class TestSessionKey:
    def test_parses_job_name(self):
        key = "0198abc-def:qwen-drain:qd0828-x-onepassword-embervm"
        assert (
            drain_console.job_name_from_session_key(key)
            == "qd0828-x-onepassword-embervm"
        )

    def test_rejects_foreign_keys(self):
        assert drain_console.job_name_from_session_key("plain-uuid") is None
        assert drain_console.job_name_from_session_key(None) is None
        assert drain_console.job_name_from_session_key("wf:qwen-drain:") is None


class TestComposeJobs:
    def test_joins_latest_session_and_counts_attempts(self):
        jobs = [_job(last_run_at=NOW, last_status="error")]
        sessions = [
            {
                "id": 12,
                "local_session_id": "wf2:qwen-drain:qd-example",
                "workflow_id": "wf2",
                "status": "completed",
                "created_at": NOW,
            },
            {
                "id": 7,
                "local_session_id": "wf1:qwen-drain:qd-example",
                "workflow_id": "wf1",
                "status": "completed",
                "created_at": NOW - timedelta(hours=1),
            },
        ]
        last_turns = {12: {"terminal_reason": "stop", "calls": 8, "cost_usd": 0.0}}
        entries = drain_console.compose_jobs(jobs, sessions, last_turns, {}, NOW)
        assert len(entries) == 1
        session = entries[0]["session"]
        assert session["id"] == 12
        assert session["attempts"] == 2
        assert session["calls"] == 8

    def test_prompt_and_summary_heads_are_first_line_only(self):
        jobs = [_job(last_summary="line one\nline two", last_run_at=NOW)]
        entries = drain_console.compose_jobs(jobs, [], {}, {}, NOW)
        assert entries[0]["prompt_head"] == "Audit the thing"
        assert entries[0]["summary_head"] == "line one"

    def test_sort_running_then_due_oldest_first_then_finished_newest_first(self):
        jobs = [
            _job(
                name="done-old", last_run_at=NOW - timedelta(hours=2), last_status="ok"
            ),
            _job(
                name="done-new",
                last_run_at=NOW - timedelta(minutes=1),
                last_status="ok",
            ),
            _job(name="due-b", next_run_at=NOW - timedelta(minutes=1)),
            _job(name="due-a", next_run_at=NOW - timedelta(minutes=30)),
            _job(
                name="live",
                locked_by="qwen-drainer",
                locked_at=NOW,
                next_run_at=NOW - timedelta(minutes=2),
            ),
        ]
        entries = drain_console.compose_jobs(jobs, [], {}, {}, NOW)
        assert [e["name"] for e in entries] == [
            "live",
            "due-a",
            "due-b",
            "done-new",
            "done-old",
        ]

    def test_queue_counts(self):
        jobs = [
            _job(name="a", next_run_at=NOW - timedelta(minutes=1)),
            _job(name="b", last_run_at=NOW, last_status="error"),
        ]
        entries = drain_console.compose_jobs(jobs, [], {}, {}, NOW)
        counts = drain_console.queue_counts(entries)
        assert counts["due"] == 1
        assert counts["error"] == 1
        assert counts["running"] == 0


def _cycle(**overrides) -> dict:
    cycle = {
        "workflow_uuid": "wf-live",
        "status": "PENDING",
        "created_at": int((NOW - timedelta(minutes=45)).timestamp() * 1000),
        "updated_at": int((NOW - timedelta(minutes=44)).timestamp() * 1000),
        "application_version": "v1",
    }
    cycle.update(overrides)
    return cycle


class TestComposeLane:
    def test_thresholds_are_the_documented_numbers(self):
        # Grepped and asserted on: the rail's meaning changes with these.
        assert drain_console.QUIET_AFTER_SECONDS == 120
        assert drain_console.WEDGED_AFTER_SECONDS == 600

    def test_fresh_checkpoint_is_running(self):
        stats = {
            "wf-live": {
                "last_ms": int((NOW - timedelta(seconds=6)).timestamp() * 1000),
                "steps": 400,
                "claims": 3,
                "finishes": 2,
                "last_step": "start_agent_session",
            }
        }
        lane = drain_console.compose_lane([_cycle()], stats, "v1", 0, True, NOW)
        assert lane["state"] == "running"
        assert lane["cycle"]["checkpoint_age_seconds"] == 6
        assert lane["cycle"]["last_step"] == "start_agent_session"

    def test_silence_past_quiet_then_wedged(self):
        quiet_stats = {
            "wf-live": {
                "last_ms": int((NOW - timedelta(seconds=180)).timestamp() * 1000)
            }
        }
        wedged_stats = {
            "wf-live": {
                "last_ms": int((NOW - timedelta(minutes=34)).timestamp() * 1000)
            }
        }
        assert (
            drain_console.compose_lane([_cycle()], quiet_stats, "v1", 0, True, NOW)[
                "state"
            ]
            == "quiet"
        )
        assert (
            drain_console.compose_lane([_cycle()], wedged_stats, "v1", 0, True, NOW)[
                "state"
            ]
            == "wedged"
        )

    def test_no_steps_falls_back_to_updated_at(self):
        # Tonight's wedge shape: PENDING with stale step activity, and a
        # fresh dequeue must not be reaped for having no steps yet.
        lane = drain_console.compose_lane([_cycle()], {}, "v1", 0, True, NOW)
        assert lane["state"] == "wedged"
        assert lane["cycle"]["checkpoint_age_seconds"] == 44 * 60

    def test_enqueued_version_mismatch_is_stranded(self):
        cycle = _cycle(status="ENQUEUED", application_version="old")
        lane = drain_console.compose_lane([cycle], {}, "v2", 0, True, NOW)
        assert lane["state"] == "stranded"

    def test_enqueued_unresolvable_server_version_is_waiting(self):
        # "" means cannot tell, never evidence of stranding.
        cycle = _cycle(status="ENQUEUED", application_version="old")
        lane = drain_console.compose_lane([cycle], {}, "", 0, True, NOW)
        assert lane["state"] == "waiting"

    def test_no_live_cycle_states(self):
        done = _cycle(status="SUCCESS")
        assert (
            drain_console.compose_lane([done], {}, "v1", 5, True, NOW)["state"]
            == "waiting"
        )
        assert (
            drain_console.compose_lane([done], {}, "v1", 0, True, NOW)["state"]
            == "idle"
        )
        assert (
            drain_console.compose_lane([done], {}, "v1", 0, False, NOW)["state"]
            == "off"
        )

    def test_error_renders_unknown_not_idle(self):
        lane = drain_console.compose_lane([], {}, "v1", 0, True, NOW, error="boom")
        assert lane["state"] == "unknown"
        assert lane["error"] == "boom"


class TestRecentCycles:
    def test_skips_live_and_computes_duration(self):
        live = _cycle()
        done = _cycle(
            workflow_uuid="wf-done",
            status="SUCCESS",
            created_at=int((NOW - timedelta(minutes=30)).timestamp() * 1000),
            updated_at=int((NOW - timedelta(minutes=20)).timestamp() * 1000),
        )
        stats = {"wf-done": {"finishes": 4}}
        recent = drain_console.compose_recent_cycles([live, done], stats)
        assert [c["workflow_id"] for c in recent] == ["wf-done"]
        assert recent[0]["duration_seconds"] == 600
        assert recent[0]["finishes"] == 4


class TestReapAfter:
    def test_mirrors_the_reaper_formula(self):
        settings = {"turn_timeout_seconds": 1800, "max_jobs_per_cycle": 15}
        assert drain_console.reap_after_seconds(settings) == 1800 + 15 * 60 + 600
