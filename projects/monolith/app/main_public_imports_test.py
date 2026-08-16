"""Import-closure guard for the public app (ADR 004 Layer 1+4).

The public service must ship a pruned image that does NOT contain the private
write-path modules or the ClickHouse client/creds. This test imports
``app.main_public`` in a FRESH subprocess (so module state leaked into
``sys.modules`` by other tests in the same process cannot mask a regression) and
asserts that none of the forbidden private modules ended up in the child's
``sys.modules``.

If this test fails, a module-level import somewhere in the public register chain
re-introduced a private dependency: find the offending ``import`` and make it
lazy (move it inside the function that needs it) rather than weakening the
forbidden list below.

``import pytest`` is intentional even though no fixtures are used: it keeps
gazelle's dependency inference attaching ``@pip//pytest`` to this target.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest  # noqa: F401  (keeps the gazelle pytest dep; see module docstring)

# Private surface that must never land in the public import closure. Each entry
# is matched as a module name OR a dotted prefix (so "chat" also forbids
# "chat.anything").
FORBIDDEN_MODULES = [
    # semgrep_scan: only .client (the fc-invoke HTTP client) is public-safe,
    # for the ember semgrep demo; the rest of the package is private-only.
    "semgrep_scan.mcp",
    "semgrep_scan.report",
    "semgrep_scan.router",
    # Private domains.
    "chat",
    "agent",
    "goosecracker",
    "scheduler",
    "moving",
    # Firecracker demos: an authenticated-only router that wraps the private
    # sandbox/semgrep/goosecracker handlers; it must never enter the public
    # closure (it is not globbed into the public binary either).
    "demos",
    # Trips read path (models + read_router) is public; the write/heavy path
    # must stay out of the public closure (pillow/boto3/defusedxml).
    "trips.ingest_router",
    "trips.ingest",
    "trips.s3",
    "trips.exif",
    "trips.transform",
    "trips.backfill",
    # Private knowledge routers / write paths.
    "knowledge.router",
    "knowledge.tasks_router",
    "knowledge.gaps",
    "knowledge.ingest_queue",
    "knowledge.mcp",
    # Heavy knowledge write/maintenance internals this refactor removed from
    # the public closure.
    "knowledge.service",
    "knowledge.layout",
    # ClickHouse client + writer path + private home paths.
    "home.observability.clickhouse",
    "home.observability.slo",
    "home.observability.rollup",
    "home.observability.stats",
    "home.schedule",
    "home.schedule_router",
    # Private cluster domain: the read-only k8s client and the k8s-* debug MCP
    # surface. Reachable from the private stats endpoint via cluster.api, never
    # from the public app.
    "cluster.api",
    "cluster.kubernetes",
    "cluster.mcp",
    "cluster.summarize",
    # Worldcup write/heavy path: the scheduled poll job, the odds HTTP client,
    # and the Monte Carlo sim. The public binary mounts only worldcup.router
    # (read-only summary); these must never enter its closure (scheduler/httpx).
    "worldcup.jobs",
    "worldcup.client",
    "worldcup.sim",
    # Ember synthetic prober and private trigger: the public tier reads the probe
    # latch (ember_public.synthetic) to answer /api/health, but the prober and
    # internal endpoint that drive the demos run only in private images. Pruned from the public
    # file set in BUILD; this locks the split so a future health.py edit
    # cannot quietly pull the prober into the public closure.
    "ember_public.synthetic_probe",
    "ember_public.synthetic_router",
]

# Snippet run in the child: import the public app, then dump every loaded module
# name as JSON on stdout so the parent can assert on the closure.
_SNIPPET = (
    "import app.main_public; import json, sys; print(json.dumps(list(sys.modules)))"
)


def _loaded_modules() -> set[str]:
    """Import ``app.main_public`` in a fresh process; return its sys.modules."""
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
        "child failed to import app.main_public:\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
    return set(json.loads(proc.stdout.strip().splitlines()[-1]))


def test_public_import_closure_excludes_private_modules() -> None:
    """No forbidden private module is present in the public import closure."""
    loaded = _loaded_modules()
    offenders = sorted(
        forbidden
        for forbidden in FORBIDDEN_MODULES
        if forbidden in loaded
        or any(m == forbidden or m.startswith(forbidden + ".") for m in loaded)
    )
    assert not offenders, (
        "app.main_public pulled forbidden private modules into its import "
        f"closure: {offenders}. Make the offending import lazy (move it inside "
        "the function that needs it); do not weaken FORBIDDEN_MODULES."
    )
