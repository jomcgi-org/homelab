"""Import-closure guard for the agents app (ADR 004 Layer 1+4).

The agents service must ship a pruned image that does NOT contain the private
domain modules or their credentials. This test imports ``app.agents_main`` in a
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

import json
import os
import subprocess
import sys

import pytest  # noqa: F401  (keeps the gazelle pytest dep; see module docstring)

# Private surface that must never land in the agents import closure. Each entry
# is matched as a module name OR a dotted prefix. Cluster is intentionally
# broad: the agent tier must have no path to Kubernetes credentials or cluster
# RBAC.
FORBIDDEN_MODULES = [
    # Private domains.
    "agent",
    "agent_sessions",
    "cluster",
    "demos",
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
    "knowledge.layout",
    "knowledge.router",
    "knowledge.service",
    "knowledge.tasks_router",
    # Private home scheduling and observability writer paths.
    "home.schedule",
    "home.schedule_router",
    "home.observability.slo",
    "home.observability.rollup",
    "home.observability.stats",
    "home.observability.traces",
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
