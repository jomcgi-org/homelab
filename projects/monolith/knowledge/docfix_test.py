"""Tests for demand-driven documentation review jobs."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import text
from sqlmodel import Session, create_engine

from knowledge import docfix
import swarm.drainer as drainer


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'docfix.db'}")
    with Session(engine) as session:
        session.execute(
            text(
                """
                CREATE TABLE routine_jobs (
                    name TEXT PRIMARY KEY,
                    routine_kind TEXT NOT NULL,
                    interval_secs INTEGER,
                    next_run_at TIMESTAMP,
                    last_run_at TIMESTAMP,
                    last_status TEXT,
                    last_summary TEXT,
                    locked_by TEXT,
                    locked_at TIMESTAMP,
                    ttl_secs INTEGER,
                    payload TEXT,
                    created_by TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        session.commit()
        yield session


def test_review_template_contains_every_gate_and_exact_prs():
    prompt = docfix.render_docfix_review_prompt([5527, 5570], auto_merge=False)

    assert "Review exactly these PRs: 5527, 5570 (at most 10)" in prompt
    for gate in ("Scope gate", "Size gate", "Evidence gate", "Text gate", "CI gate"):
        assert gate in prompt
    for path_glob in (
        *docfix.DOCFIX_ALLOWED_PATH_GLOBS,
        *docfix.DOCFIX_PROTECTED_PATH_GLOBS,
    ):
        assert f"`{path_glob}`" in prompt
    assert "add label `needs-human`" in prompt
    assert "all SUCCESS" in prompt
    assert "skip this run without comment" in prompt
    assert "never edit the PR branch" in prompt
    assert "gh label create needs-human" in prompt
    assert "gh label create docfix-verified" in prompt
    assert '"skipped_pending": [..]' in prompt


def test_review_template_renders_merge_switch():
    comment_only = docfix.render_docfix_review_prompt([101], auto_merge=False)
    merging = docfix.render_docfix_review_prompt([202], auto_merge=True)

    assert "AUTO_MERGE=false" in comment_only
    assert "docfix-review: would merge (verified against main <sha7>)" in comment_only
    assert "gh pr merge <n>" not in comment_only
    assert "AUTO_MERGE=true" in merging
    assert "gh pr merge <n> --auto --rebase" in merging
    assert "docfix-review: verified against main <sha7>, queued" in merging
    assert "`--admin`" in merging
    assert "never squash" in merging


def test_schedule_registers_one_shot_and_debounces(session, monkeypatch):
    monkeypatch.setenv("KG_DOCFIX_REVIEW_ENABLED", "true")
    monkeypatch.setenv("DRAINER_DOCFIX_AUTO_MERGE", "false")

    assert docfix.schedule_docfix_review(session, [31, 32], 600) is True
    row = session.execute(text("SELECT * FROM routine_jobs")).one()
    payload = json.loads(row.payload)
    assert row.name.startswith("docfix-review:")
    assert row.routine_kind == "qwen-drain"
    assert row.interval_secs is None
    assert row.next_run_at is not None
    assert payload == {
        "prompt": docfix.render_docfix_review_prompt([31, 32], auto_merge=False),
        "pr_numbers": [31, 32],
        "repo": "jomcgi-org/homelab",
        "branch": "main",
    }
    assert docfix.schedule_docfix_review(session, [33], 0) is False


def test_schedule_debounces_recent_completion(session, monkeypatch):
    monkeypatch.setenv("KG_DOCFIX_REVIEW_ENABLED", "true")
    session.execute(
        text(
            """
            INSERT INTO routine_jobs
                (name, routine_kind, last_run_at, payload, created_by)
            VALUES
                ('docfix-review:old', 'qwen-drain', :last_run_at, '{}', 'test')
            """
        ),
        {"last_run_at": datetime.now(timezone.utc) - timedelta(minutes=5)},
    )
    session.commit()

    assert docfix.schedule_docfix_review(session, [34], 0) is False


def test_schedule_allows_review_after_completed_debounce(session, monkeypatch):
    monkeypatch.setenv("KG_DOCFIX_REVIEW_ENABLED", "true")
    session.execute(
        text(
            """
            INSERT INTO routine_jobs
                (name, routine_kind, last_run_at, payload, created_by)
            VALUES
                ('docfix-review:old', 'qwen-drain', :last_run_at, '{}', 'test')
            """
        ),
        {"last_run_at": datetime.now(timezone.utc) - timedelta(minutes=31)},
    )
    session.commit()

    assert docfix.schedule_docfix_review(session, [35], 0) is True


def test_prune_completed_reviews_only_removes_expired_rows(session):
    now = datetime.now(timezone.utc)
    for name, last_run_at in (
        ("docfix-review:recent", now - timedelta(minutes=5)),
        ("docfix-review:expired", now - timedelta(minutes=31)),
    ):
        session.execute(
            text(
                """
                INSERT INTO routine_jobs
                    (name, routine_kind, last_run_at, payload, created_by)
                VALUES (:name, 'qwen-drain', :last_run_at, '{}', 'test')
                """
            ),
            {"name": name, "last_run_at": last_run_at},
        )
    session.commit()

    assert docfix.prune_completed_docfix_reviews(session) == 1
    names = session.execute(text("SELECT name FROM routine_jobs")).scalars().all()
    assert names == ["docfix-review:recent"]


def test_schedule_is_noop_when_master_switch_is_off(session, monkeypatch):
    monkeypatch.setenv("KG_DOCFIX_REVIEW_ENABLED", "false")

    assert docfix.schedule_docfix_review(session, [35], 0) is False
    assert session.execute(text("SELECT * FROM routine_jobs")).all() == []


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_find_reviewable_docfix_prs_filters_labels_and_titles(session, monkeypatch):
    monkeypatch.setenv("KG_DOCFIX_REVIEW_ENABLED", "true")
    monkeypatch.setenv("GITHUB_API_TOKEN", "test-token")
    monkeypatch.setattr(
        docfix,
        "_github_get",
        lambda _url: _Response(
            [
                {
                    "number": 41,
                    "title": "docs: current wording",
                    "head": {"ref": "docfix/current-wording"},
                    "labels": [{"name": "qwen-agent-for-review"}],
                },
                {
                    "number": 42,
                    "title": "docs: already reviewed",
                    "head": {"ref": "docfix/already-reviewed"},
                    "labels": [
                        {"name": "qwen-agent-for-review"},
                        {"name": "docfix-verified"},
                    ],
                },
                {
                    "number": 43,
                    "title": "fix: code",
                    "head": {"ref": "docfix/code"},
                    "labels": [{"name": "qwen-agent-for-review"}],
                },
                {
                    "number": 44,
                    "title": "docs: unrelated documentation",
                    "head": {"ref": "docs/unrelated"},
                    "labels": [{"name": "qwen-agent-for-review"}],
                },
            ]
        ),
    )

    assert docfix.find_reviewable_docfix_prs(session) == [41]


def _drainer_settings():
    return {
        "enabled": True,
        "max_jobs_per_cycle": 3,
        "turn_timeout_seconds": 1800,
        "job_kinds": ("qwen-drain", "kg-drain"),
        "kg_max_jobs_per_day": 40,
        "repo": "jomcgi-org/homelab",
        "branch": "main",
        "reasoning": True,
    }


class _FakeDBOS:
    workflow_id = "workflow-docfix"


def test_drainer_schedules_review_after_docfix_pr_url(monkeypatch):
    jobs = iter(
        [
            {
                "name": "docfix:abc",
                "routine_kind": "qwen-drain",
                "payload": {"prompt": "fix docs"},
            },
            None,
        ]
    )
    scheduled = []
    monkeypatch.setattr(drainer, "pin_drainer_settings", _drainer_settings)
    monkeypatch.setattr(drainer, "DBOS", _FakeDBOS)
    monkeypatch.setattr(drainer, "sweep_kg_raws", lambda: 0)
    monkeypatch.setattr(drainer, "claim_drainer_job", lambda *_args: next(jobs))
    monkeypatch.setattr(drainer, "start_agent_session", lambda *_args: 1)
    monkeypatch.setattr(
        drainer,
        "_await_turn",
        lambda *_args: {
            "result_text": "https://github.com/jomcgi-org/homelab/pull/812",
            "terminal_reason": "stop",
        },
    )
    monkeypatch.setattr(drainer, "finish_drainer_job", lambda *_args: True)
    monkeypatch.setattr(
        drainer,
        "schedule_docfix_review_for_completion",
        lambda result: scheduled.append(result) or True,
    )
    monkeypatch.setattr(drainer, "destroy_drainer_session", lambda *_args: True)

    assert drainer.drain_cycle.__wrapped__()["processed"] == 1
    assert scheduled == ["https://github.com/jomcgi-org/homelab/pull/812"]


def test_completion_trigger_extracts_pr_and_uses_ten_minute_delay(session, monkeypatch):
    scheduled = []
    monkeypatch.setattr("core.db.get_engine", lambda: session.get_bind())
    monkeypatch.setattr(
        drainer,
        "schedule_docfix_review",
        lambda _session, pr_numbers, delay_seconds: (
            scheduled.append((pr_numbers, delay_seconds)) or True
        ),
    )

    assert (
        drainer.schedule_docfix_review_for_completion.__wrapped__(
            "done: https://github.com/jomcgi-org/homelab/pull/913"
        )
        is True
    )
    assert scheduled == [([913], 600)]


def test_sweep_schedules_reviewable_prs(session, monkeypatch):
    scheduled = []
    monkeypatch.setenv("KG_DOCFIX_REVIEW_ENABLED", "true")
    monkeypatch.setenv("GITHUB_API_TOKEN", "test-token")
    monkeypatch.setattr("core.db.get_engine", lambda: session.get_bind())
    monkeypatch.setattr("knowledge.api.sweep_unqueued_raws", lambda *_args: 4)
    monkeypatch.setattr(
        docfix,
        "_github_get",
        lambda _url: _Response(
            [
                {
                    "number": 71,
                    "title": "docs: review me",
                    "head": {"ref": "docfix/review-me"},
                    "labels": [{"name": "qwen-agent-for-review"}],
                }
            ]
        ),
    )
    monkeypatch.setattr(
        drainer,
        "schedule_docfix_review",
        lambda _session, pr_numbers, delay_seconds: (
            scheduled.append((pr_numbers, delay_seconds)) or True
        ),
    )

    assert drainer.sweep_kg_raws.__wrapped__() == 4
    assert scheduled == [([71], 0)]


def test_docfix_review_summary_is_preserved_as_json():
    result = """Reviewed docs.\n```json
{"reviewed": 3, "queued": [1], "verified": [2], "needs_human": [3], "skipped_pending": []}
```"""

    assert json.loads(drainer._docfix_review_summary(result)) == {
        "reviewed": 3,
        "queued": [1],
        "verified": [2],
        "needs_human": [3],
        "skipped_pending": [],
    }


def test_helm_deploy_values_render_both_review_flags_false():
    root = next(
        parent
        for parent in Path(__file__).resolve().parents
        if (parent / "projects/monolith/chart/Chart.yaml").exists()
    )
    helm = os.environ.get("HELM_BIN", "helm")
    deploy_values = os.environ.get(
        "DEPLOY_VALUES", str(root / "projects/monolith/deploy/values.yaml")
    )
    result = subprocess.run(
        [
            helm,
            "template",
            "monolith",
            str(root / "projects/monolith/chart"),
            "--namespace",
            "monolith",
            "--values",
            str(root / "projects/monolith/chart/values.yaml"),
            "--values",
            deploy_values,
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert (
        'name: DRAINER_DOCFIX_AUTO_MERGE\n              value: "false"' in result.stdout
    )
    assert (
        'name: KG_DOCFIX_REVIEW_ENABLED\n              value: "false"' in result.stdout
    )
