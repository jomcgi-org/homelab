"""Import-closure guard for the agents app (ADR 004 Layer 1+4).

The agents service must ship a pruned image that does NOT contain the private
domain modules or their credentials. The sole Kubernetes exception is the
standalone ``agent_kubernetes`` package, which has no mutation methods and does
not import the private ``cluster`` domain. This test imports ``app.agents_main`` in a
FRESH subprocess (so module state leaked into ``sys.modules`` by other tests in
the same process cannot mask a regression) and asserts that none of the
forbidden private modules ended up in the child's ``sys.modules``.

If this test fails, a module-level import somewhere in the agents register
chain re-introduced a private dependency: find the offending ``import`` and
make it lazy (move it inside the function that needs it) rather than weakening
the forbidden list below.

``import pytest`` is intentional even though no fixtures are used: it keeps
gazelle's dependency inference attaching ``@pip//pytest`` to this target.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest  # noqa: F401  (keeps the gazelle pytest dep; see module docstring)
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from auth.principal import Authority, Principal, PrincipalKind, anonymous_principal
from knowledge.models import AgentReportWriteFailure, Note, RawInput
from knowledge.raw_write import write_raw

# Private surface that must never land in the agents import closure. Each entry
# is matched as a module name OR a dotted prefix. Cluster stays intentionally
# broad: the agent tier may import only agent_kubernetes, never the private
# client's mutation helpers or broader credential paths.
FORBIDDEN_MODULES = [
    # Private domains.
    "agent",
    "agent_sessions",
    "factory",
    "cluster",
    "goosecracker",
    "moving",
    "sandbox",
    "scheduler",
    "shotter",
    "trips",
    "updates",
    # chat.models and chat.outbox are shared outbox producer infrastructure;
    # no other chat modules are imported.
    "chat.acl",
    "chat.agent",
    "chat.ambient_analysis",
    "chat.api",
    "chat.attention",
    "chat.attention_log",
    "chat.autopilot_job",
    "chat.backfill",
    "chat.bot",
    "chat.changelog",
    "chat.channel_data",
    "chat.cluster_agent",
    "chat.digest",
    "chat.directive_admin",
    "chat.directives",
    "chat.explorer",
    "chat.jobs",
    "chat.leader",
    "chat.module",
    "chat.observer",
    "chat.observer_job",
    "chat.orchestrator",
    "chat.orchestrator_client",
    "chat.orchestrator_plan",
    "chat.reminders",
    "chat.reply_repair_log",
    "chat.reply_sanitize",
    "chat.router",
    "chat.safeguards",
    "chat.safeguards_forest",
    "chat.safeguards_train_job",
    "chat.sse",
    "chat.store",
    "chat.summarizer",
    "chat.vision",
    "chat.web_search",
    "chat.whatsapp_calendar",
    "chat.whatsapp_capabilities",
    "chat.whatsapp_digest",
    "chat.whatsapp_inbound",
    "chat.whatsapp_intents",
    "chat.whatsapp_outbox",
    "chat.whatsapp_session",
    "chat.whatsapp_timeparse",
    # Private monolith entrypoints and registries.
    "app.main",
    "app.main_domain",
    "app.jobs_main",
    "app.mcp_app",
    "app.modules_private",
    "app.progress_main",
    "core.mcp_app",
    # Private knowledge routers, maintenance code, and write-path internals.
    "knowledge.gaps",
    "knowledge.ingest_queue",
    # knowledge.raw_write is allowed: it only inserts raw metadata via SQLModel.
    "knowledge.layout",
    "knowledge.publish",
    "knowledge.router",
    "knowledge.service",
    "knowledge.tasks_router",
    # Private home scheduling and observability writer paths.
    "home.schedule",
    "home.schedule_router",
    "home.observability.slo",
    "home.observability.rollup",
    "home.observability.stats",
    # Other domains outside the agents tier allowlist.
    "artifact",
    "campsites",
    "chat_public",
    "dr_jobs",
    "ember_public",
    "faas",
    "grimoire",
    "grimoire_chat",
    "hikes",
    "home",
    "semgrep_scan",
    "ships",
    "stars",
    "swarm",
    "worldcup",
]

# Snippet run in the child: import the agents app, then dump every loaded module
# name as JSON on stdout so the parent can assert on the closure.
_SNIPPET = (
    "import app.agents_main; import json, sys; print(json.dumps(list(sys.modules)))"
)


def _loaded_modules() -> set[str]:
    """Import ``app.agents_main`` in a fresh process; return its sys.modules."""
    env = dict(os.environ)
    # Propagate the test runner's import roots so the child can import ``app``
    # regardless of how the Bazel py launcher set up the parent's sys.path.
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    proc = subprocess.run(
        [sys.executable, "-c", _SNIPPET],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, (
        "child failed to import app.agents_main:\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    return set(json.loads(proc.stdout.strip().splitlines()[-1]))


def test_agents_import_closure_excludes_private_modules() -> None:
    """No forbidden private module is present in the agents import closure."""
    loaded = _loaded_modules()
    offenders = sorted(
        forbidden
        for forbidden in FORBIDDEN_MODULES
        if forbidden in loaded
        or any(m == forbidden or m.startswith(forbidden + ".") for m in loaded)
    )
    assert not offenders, (
        "app.agents_main pulled forbidden private modules into its import "
        f"closure: {offenders}. Make the offending import lazy (move it inside "
        "the function that needs it); do not weaken FORBIDDEN_MODULES."
    )


def test_report_distress_is_in_the_agent_catalogue():
    """Distress reporting is essential for an agent tier, so pin it.

    It reaches Discord through shared.notify rather than agent.api, which is
    what lets it live here at all: the agents binary prunes agent/**, and the
    import that used to reach it was function-local, so losing it again would
    fail at call time rather than at build time.
    """

    import app.agents_main as agents_main

    assert "report_distress" in agents_main.AGENT_TOOL_NAMES


def test_agent_mcp_anonymous_boundary_returns_401_before_tools():
    import app.agents_main as agents_main

    downstream_called = False
    messages = []

    async def downstream(_scope, _receive, _send):
        nonlocal downstream_called
        downstream_called = True

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    gate = agents_main._AuthenticatedPrincipalGate(downstream)
    with patch.object(
        agents_main, "current_principal", return_value=anonymous_principal()
    ):
        asyncio.run(gate({"type": "http"}, receive, send))

    assert downstream_called is False
    assert messages[0]["type"] == "http.response.start"
    assert messages[0]["status"] == 401


def test_only_narrow_kubernetes_tools_join_the_agent_catalogue():
    import app.agents_main as agents_main

    assert agents_main.AGENT_TOOL_NAMES == (
        "search_knowledge",
        "report_knowledge",
        "dispute_fact",
        "report_distress",
        "post_message",
        "read_board",
        "ack_message",
        "kubernetes_read",
        "kubernetes_pod_logs",
    )
    loaded = _loaded_modules()
    assert "agent_kubernetes.client" in loaded
    assert not any(
        module == "cluster" or module.startswith("cluster.") for module in loaded
    )


@pytest.fixture(name="agents_db")
def agents_db_fixture(tmp_path, monkeypatch):
    """Provide the agents catalogue with a file-backed raw-input database."""
    engine = create_engine(f"sqlite:///{tmp_path / 'agents-report.db'}")
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    uploads: dict[str, str] = {}
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            session.execute(
                text(
                    """
                    CREATE TABLE routine_jobs (
                        name TEXT PRIMARY KEY,
                        routine_kind TEXT NOT NULL,
                        interval_secs INTEGER,
                        next_run_at TIMESTAMP,
                        payload TEXT,
                        created_by TEXT
                    )
                    """
                )
            )
            session.commit()
        monkeypatch.setattr(
            "knowledge.raw_write.upload_raw",
            lambda raw_id, content: uploads.__setitem__(raw_id, content),
        )
        yield SimpleNamespace(engine=engine, uploads=uploads)
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


def _agents_principal() -> Principal:
    return Principal(
        subject="agent:ember-guest",
        actor=(),
        scope=(),
        groups=(),
        email=None,
        kind=PrincipalKind.WORKLOAD,
        authority=Authority.DELEGATED,
    )


def test_reporting_tools_run_from_agents_import_closure(agents_db) -> None:
    """All reporting tools run without reaching the private ingest module."""
    import app.agents_main as agents_main

    loaded = _loaded_modules()
    assert "knowledge.raw_write" in loaded
    assert "knowledge.ingest_queue" not in loaded
    agents_main.build_agent_mcp_app(resolver=object())

    with Session(agents_db.engine) as session:
        session.add(
            Note(
                note_id="agents-closure-fact",
                path="agents-closure-fact.md",
                title="Agents closure fact",
                content_hash="agents-closure-fact-hash",
                content="Current body",
                type="fact",
            )
        )
        session.commit()

    with (
        patch("knowledge.mcp.get_engine", return_value=agents_db.engine),
        patch("knowledge.mcp.current_principal", return_value=_agents_principal()),
        patch("knowledge.mcp._notify", AsyncMock(return_value={"ok": True})),
    ):
        report_result = asyncio.run(
            agents_main.report_knowledge(
                "Agents can persist reports",
                evidence=["projects/monolith/app/agents_main.py"],
            )
        )
        dispute_result = asyncio.run(
            agents_main.dispute_fact(
                "agents-closure-fact",
                "The checked-out source contradicts this",
            )
        )
        distress_result = asyncio.run(
            agents_main.report_distress("Agent is blocked", "blocked")
        )

    assert report_result["status"] == "queued"
    assert dispute_result["status"] == "disputed"
    assert distress_result["status"] == "notified"
    assert "knowledge.ingest_queue" not in sys.modules
    with Session(agents_db.engine) as session:
        raw = session.exec(
            select(RawInput).where(RawInput.raw_id == report_result["raw_id"])
        ).one()
        jobs = session.execute(text("SELECT * FROM routine_jobs")).all()
        assert raw.extra["evidence"] == ["projects/monolith/app/agents_main.py"]
        assert raw.extra["status"] == "queued"
        assert len(jobs) == 2
        assert raw.raw_id in agents_db.uploads
        assert dispute_result["raw_id"] in agents_db.uploads
        assert distress_result["intervention_id"] in agents_db.uploads


def test_raw_write_coerces_evidence_to_json_lists(agents_db) -> None:
    """Evidence is always a JSON list or null, including string input."""
    with Session(agents_db.engine) as session:
        with_evidence, _ = write_raw(
            session,
            content="report with evidence",
            source="agent-report",
            scope="repo:jomcgi-org/homelab",
            evidence=["one", "two"],
            status="queued",
        )
        without_evidence, _ = write_raw(
            session,
            content="report without evidence",
            source="agent-report",
            scope="repo:jomcgi-org/homelab",
            evidence=None,
            status="queued",
        )
        string_evidence, _ = write_raw(
            session,
            content="report with string evidence",
            source="agent-report",
            scope="repo:jomcgi-org/homelab",
            evidence="one",
            status="queued",
        )
        raw_ids = (
            with_evidence.raw_id,
            without_evidence.raw_id,
            string_evidence.raw_id,
        )

    with Session(agents_db.engine) as session:
        stored = [
            session.exec(select(RawInput).where(RawInput.raw_id == raw_id)).one()
            for raw_id in raw_ids
        ]
        assert stored[0].extra["evidence"] == ["one", "two"]
        assert stored[1].extra["evidence"] is None
        assert stored[2].extra["evidence"] == ["one"]


def test_report_knowledge_returns_structured_write_error(agents_db) -> None:
    """A write failure has a safe response and a durable health record."""
    import knowledge.mcp as knowledge_mcp

    with (
        patch("knowledge.mcp.get_engine", return_value=agents_db.engine),
        patch("knowledge.mcp.current_principal", return_value=_agents_principal()),
        patch(
            "knowledge.mcp.persist_raw_with_status",
            side_effect=RuntimeError("postgresql://secret@db/write failed"),
        ),
    ):
        result = asyncio.run(knowledge_mcp.report_knowledge("A report"))

    assert result == {"error": "report could not be persisted: RuntimeError"}
    with Session(agents_db.engine) as session:
        failure = session.exec(select(AgentReportWriteFailure)).one()
        assert failure.reporter_kind == "workload"
        assert failure.error_type == "RuntimeError"


def test_distress_mirror_failure_replay_and_single_notification(agents_db) -> None:
    """Mirror availability never breaks or duplicates the human path."""
    import knowledge.mcp as knowledge_mcp

    mirror = AsyncMock(
        side_effect=[RuntimeError("board unavailable"), {"status": "posted"}]
    )
    notify = AsyncMock(return_value={"ok": True})
    with (
        patch("knowledge.mcp.get_engine", return_value=agents_db.engine),
        patch("knowledge.mcp.current_principal", return_value=_agents_principal()),
        patch("knowledge.mcp.mirror_distress", mirror),
        patch("knowledge.mcp._notify", notify),
    ):
        first = asyncio.run(
            knowledge_mcp.report_distress(
                "Agent is blocked",
                "blocked",
                "same durable report",
                "inspect dependency",
            )
        )
        replay = asyncio.run(
            knowledge_mcp.report_distress(
                "Agent is blocked",
                "blocked",
                "same durable report",
                "inspect dependency",
            )
        )

    assert first["status"] == "notified"
    assert replay == {
        "intervention_id": first["intervention_id"],
        "status": "recorded",
    }
    assert mirror.await_count == 2
    notify.assert_awaited_once()


def test_distress_mirror_timeout_does_not_delay_notification(agents_db) -> None:
    import knowledge.mcp as knowledge_mcp

    async def stalled_mirror(**_kwargs):
        await asyncio.sleep(1)

    notify = AsyncMock(return_value={"ok": True})
    with (
        patch("knowledge.mcp.get_engine", return_value=agents_db.engine),
        patch("knowledge.mcp.current_principal", return_value=_agents_principal()),
        patch("knowledge.mcp.mirror_distress", stalled_mirror),
        patch("knowledge.mcp._notify", notify),
        patch("knowledge.mcp._DISTRESS_MIRROR_TIMEOUT_SECONDS", 0.001),
    ):
        result = asyncio.run(
            knowledge_mcp.report_distress(
                "Agent is blocked on a timeout",
                "blocked",
                "mirror does not return",
                "inspect dependency",
            )
        )

    assert result["status"] == "notified"
    notify.assert_awaited_once()
