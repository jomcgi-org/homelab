"""Ember agent-lane synthetic probes and their health components.

The public Ember exhibits (the scale-to-zero Postgres demo and the Bazel
Skyframe query demo) were retired in 2026-10 (#6913): their pages redirect to
the recorded Firecracker replay and their routers are gone. What remains is
the private-tier production probe path that outlived them:

- ``synthetic_probe`` runs a real Codex (hourly) or Spark (manual) agent
  session and latches the outcome into ``ember_synthetic_probe``;
- ``synthetic_router`` is the ``/internal/ember/*`` trigger surface the
  ``ember-codex-session-synthetic`` and ``ember-spark-session-synthetic``
  CronWorkflows POST to (see ``app/jobs_main.py``);
- ``health`` folds the Codex latch into ``/api/health`` and ``durability``
  reads the EmberVM control plane's durability surface.

The package keeps its historical name so the ``ember_public`` domain label,
image fan-out and health component names stay stable. It is composed into the
private binary only (``app/modules_private.py``); nothing here ships in the
public image.
"""

from __future__ import annotations

from fastapi import FastAPI

__all__ = ["register"]


def register(app: FastAPI) -> None:
    """Register the internal synthetic-probe trigger routes on the private app."""
    from ember_public.synthetic_router import internal_router

    app.include_router(internal_router)
