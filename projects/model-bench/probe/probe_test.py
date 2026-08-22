from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx

from probe.cli import LoadedTask, _run_once, build_parser
from probe.fixture import apply_guest_diff, materialize_fixture
from probe.report import render_report
from probe.session import AgentSessionClient, poll_turn
from probe.spans import HOPS, bucket_spans


def test_reasoning_flag_sets_session_start_body(monkeypatch):
    args = build_parser().parse_args(["run", "--task", "sample", "--reasoning"])
    task = LoadedTask(
        spec=type(
            "Spec",
            (),
            {
                "id": "sample",
                "prompt": "hello",
                "verifier": type("Verifier", (), {"kind": "judge", "args": {}})(),
            },
        )(),
        mapping={},
    )
    start_bodies = []

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def start(self, payload):
            start_bodies.append(payload)
            return {"accepted": False, "error": "stop after start"}

        def close(self):
            pass

    monkeypatch.setattr("probe.session.AgentSessionClient", FakeClient)
    monkeypatch.setattr("probe.cli.collect_spans", lambda *_args: {})

    result = _run_once(task, 1, args)

    assert start_bodies[0]["reasoning"] is True
    assert result["reasoning"] is True


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _tiny_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "probe@example.com")
    _git(repo, "config", "user.name", "Probe Test")
    source = repo / "one" / "two" / "pkg" / "value.txt"
    source.parent.mkdir(parents=True)
    source.write_text("before\n")
    (repo / "outside.txt").write_text("outside before\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD").strip()


def test_materialize_and_apply_real_diff_with_strip_components(tmp_path: Path):
    repo, commit = _tiny_repo(tmp_path)
    snapshot = {
        "commit": commit,
        "paths": ["one/two/pkg"],
        "strip_components": 2,
    }
    fixture = tmp_path / "fixture"
    materialize_fixture(repo, snapshot, fixture)
    assert (fixture / "pkg" / "value.txt").read_text() == "before\n"

    (repo / "one" / "two" / "pkg" / "value.txt").write_text("after\n")
    diff = _git(repo, "diff", "--", "one/two/pkg/value.txt")
    result = apply_guest_diff(fixture, diff, snapshot)

    assert result.applied
    assert result.out_of_scope_files == []
    assert (fixture / "pkg" / "value.txt").read_text() == "after\n"


def test_out_of_scope_diff_is_reported_and_fixture_untouched(tmp_path: Path):
    repo, commit = _tiny_repo(tmp_path)
    snapshot = {
        "commit": commit,
        "paths": ["one/two/pkg"],
        "strip_components": 2,
    }
    fixture = tmp_path / "fixture"
    materialize_fixture(repo, snapshot, fixture)
    before = (fixture / "pkg" / "value.txt").read_text()
    (repo / "outside.txt").write_text("outside after\n")

    result = apply_guest_diff(fixture, _git(repo, "diff"), snapshot)

    assert result.applied
    assert result.out_of_scope_files == ["outside.txt"]
    assert (fixture / "pkg" / "value.txt").read_text() == before
    assert not (fixture / "outside.txt").exists()


def test_json_match_output_at_fixture_root_is_included(tmp_path: Path):
    repo, commit = _tiny_repo(tmp_path)
    snapshot = {
        "commit": commit,
        "paths": ["one/two/pkg"],
        "strip_components": 0,
    }
    fixture = tmp_path / "fixture"
    materialize_fixture(repo, snapshot, fixture)
    answer = repo / "answer.json"
    answer.write_text('{"ok": true}\n')
    _git(repo, "add", "-N", "answer.json")
    diff = _git(repo, "diff", "--", "answer.json")

    result = apply_guest_diff(fixture, diff, snapshot, extra_files=["answer.json"])

    assert result.applied
    assert json.loads((fixture / "answer.json").read_text()) == {"ok": True}


class _Clock:
    def __init__(self) -> None:
        self.now = 10.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_poll_loop_reaches_terminal_and_computes_wall():
    payloads = [
        {"session": {}, "turns": [], "pending_queue": [{"seq": 3}]},
        {
            "session": {},
            "turns": [{"seq": 3, "result_text": "done", "usage": {}}],
            "pending_queue": [],
        },
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payloads.pop(0))

    clock = _Clock()
    client = AgentSessionClient(
        "http://probe.test", transport=httpx.MockTransport(handler)
    )
    try:
        outcome = poll_turn(
            client,
            7,
            3,
            timeout_s=10,
            started_monotonic=10,
            poll_interval_s=2,
            clock=clock.clock,
            sleep=clock.sleep,
        )
    finally:
        client.close()

    assert outcome.grade == "completed"
    assert outcome.turn["result_text"] == "done"
    assert outcome.wall_s == 2


def test_poll_loop_timeout_yields_timeout_grade():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"session": {}, "turns": [], "pending_queue": [{"seq": 3}]}
        )

    clock = _Clock()
    client = AgentSessionClient(
        "http://probe.test", transport=httpx.MockTransport(handler)
    )
    try:
        outcome = poll_turn(
            client,
            7,
            3,
            timeout_s=3,
            started_monotonic=10,
            poll_interval_s=2,
            clock=clock.clock,
            sleep=clock.sleep,
        )
    finally:
        client.close()

    assert outcome.grade == "timeout"
    assert outcome.wall_s == 3


def test_span_bucketing_and_unknown_services():
    spans = [
        {"serviceName": "cloudflared", "name": "gateway", "durationNano": 2_000_000},
        {"serviceName": "context-forge", "name": "tool", "durationNano": 3_000_000},
        {"serviceName": "monolith-backend", "name": "POST", "durationNano": 4_000_000},
        {
            "serviceName": "embervm-control",
            "name": "session_restore",
            "durationNano": 5_000_000,
        },
        {"serviceName": "embervm-control", "name": "invoke", "durationNano": 7_000_000},
        {"serviceName": "pi-shim", "name": "turn", "durationNano": 11_000_000},
    ]

    result = bucket_spans(spans)

    assert result["buckets"][HOPS[0]]["total_ms"] == 2
    assert result["buckets"][HOPS[3]]["spans"] == 2
    assert result["buckets"][HOPS[4]]["service_names"] == ["pi-shim"]
    assert result["embervm_phases"] == {
        "session_create_restore_ms": 5,
        "invoke_ms": 7,
    }
    assert "pi-shim" in result["service_names_seen"]


def test_report_uses_median_over_two_reps_and_has_no_em_dash():
    rows = [
        {
            "task": "sample-01",
            "passed": True,
            "wall_s": 10,
            "num_turns": 2,
            "usage": {"input_tokens": 100, "output_tokens": 20},
            "span_buckets": {"buckets": {HOPS[2]: {"total_ms": 20}}},
        },
        {
            "task": "sample-01",
            "passed": False,
            "wall_s": 20,
            "num_turns": 4,
            "usage": {"input_tokens": 200, "output_tokens": 40},
            "span_buckets": {"buckets": {HOPS[2]: {"total_ms": 40}}},
        },
    ]

    markdown = render_report(rows)

    assert "| sample-01 | 2 | 1 | 15.0 | 3.0 | 180.0 |" in markdown
    assert "| sample-01 | monolith-backend | 30.0 |" in markdown
    assert "| task | reps |" in markdown
    assert "\N{EM DASH}" not in markdown
