"""Trigger-agnostic goose dispatch: the ``submit`` seam every trigger calls.

A trigger (the Discord ``/agent`` command, an MCP tool, a future CI hook)
supplies a task + a stable ``session`` id and calls :func:`submit`. This writes
the run ledger row and fires the fc-invoke run off as a detached task, then
returns immediately with the session, thread id, and whether the row was created
or reset (a resume). It knows nothing about Discord; result delivery is the
runner's job.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

from opentelemetry.propagate import inject

from goosecracker import runner, threads

if TYPE_CHECKING:
    # Type-only: chat.api re-exports Plan from chat.orchestrator_plan, which
    # imports goosecracker.recipe_catalog, so a runtime import here (this module
    # loads at goosecracker package init) would risk a circular import. `from
    # __future__ import annotations` above makes this annotation a string, never
    # evaluated, so TYPE_CHECKING-only is safe (import_boundaries_test).
    from chat.api import Plan

logger = logging.getLogger(__name__)


def _schedule(coro: Coroutine[Any, Any, None]) -> None:
    """Fire ``coro`` off detached, from either a worker thread or the loop.

    ``submit`` is called via ``asyncio.to_thread`` from both the Discord adapter
    and the MCP tools (a sync DB write must not run on the event loop), so there
    is usually no running loop here: run the coroutine to completion in a
    dedicated daemon thread, which needs no captured loop and lets the ~600s
    fc-invoke round-trip finish without blocking the caller. If a running loop is
    present (a future on-loop caller), schedule it there instead.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        loop.create_task(coro)
        return

    def _run() -> None:
        try:
            asyncio.run(coro)
        except Exception:
            logger.exception("goosecracker: detached run crashed")

    threading.Thread(target=_run, name="goosecracker-run", daemon=True).start()


def submit(
    task: str,
    *,
    session: str,
    recipe: str = "agent",
    tier: str = "",
    repo: str = "",
    branch: str = "main",
    git_mirror: str = "",
    git_ref: str = "",
    discord_thread: str = "",
    plan: "Plan | None" = None,
) -> dict:
    """Dispatch a goose run for ``session`` and return without waiting on it.

    Writes/updates the ledger row (state RUNNING) and kicks off the fc-invoke run
    + result delivery as a detached task. ``session`` is the stable run id (the
    Discord thread id for /agent, or a caller-generated id for MCP). ``tier``
    selects the guest model env (default -> in-cluster Qwen). ``plan`` is the
    optional runtime
    :class:`~chat.orchestrator_plan.Plan` from the DeepSeek orchestrator (Task 6);
    when present the runner delivers a rendered router + plan file via
    ``injectedContext`` instead of the baked ``recipe="agent"`` path. Returns
    ``{session, thread_id, action}`` where ``action`` is "create" or "resume".
    """
    result = threads.upsert_run(
        session,
        recipe=recipe,
        tier=tier,
        task=task,
        discord_thread=discord_thread,
    )
    # Capture the caller's active trace context (e.g. the demo page's
    # `demo.goose` span) as a W3C traceparent string. `submit` runs via
    # asyncio.to_thread, which copies the caller's contextvars, so the current
    # span is live here; the run itself executes on a fresh daemon-thread event
    # loop with no context, so we carry the string and re-attach it as a header
    # on the fc-invoke POST, letting the agent's fc-invoke spans nest under the
    # caller's trace instead of forming a detached one.
    carrier: dict[str, str] = {}
    inject(carrier)
    traceparent = carrier.get("traceparent", "")
    _schedule(
        runner.run_and_deliver(
            session,
            task=task,
            recipe=recipe,
            tier=tier,
            repo=repo,
            git_mirror=git_mirror,
            git_ref=git_ref,
            discord_thread=discord_thread,
            plan=plan,
            traceparent=traceparent,
        )
    )
    return {
        "session": session,
        "thread_id": result["thread_id"],
        "action": result["action"],
    }


def status(thread_id: str) -> dict | None:
    """Return a run's serialized ledger row by thread id, or None."""
    row = threads.get_run(thread_id)
    return threads.serialize(row) if row else None


def resume(thread_id: str) -> dict:
    """Re-dispatch a thread's stored task under its existing session.

    A thin re-submit: reads the ledger row and calls :func:`submit` again with the
    same session/recipe/tier/task, so a caller can retrigger a run without
    re-supplying its parameters. Returns ok=False when the thread is unknown.
    """
    row = threads.get_run(thread_id)
    if row is None:
        return {"ok": False, "thread_id": thread_id, "reason": "thread not found"}
    session = row.get("session_id") or thread_id
    result = submit(
        row.get("task") or "",
        session=session,
        recipe=row.get("recipe") or "agent",
        tier=row.get("tier") or "",
        discord_thread=row.get("discord_thread") or "",
    )
    return {"ok": True, **result}
