"""Run a single goose turn via the fc-invoke daemon and deliver the result.

This is the executor that replaced the deleted fc-agentd reconcile loop: instead
of writing a desired-state row for a node-4 controller to pick up, the monolith
calls the in-cluster fc-invoke daemon directly (``POST /invoke/agent/{session}``,
the same HTTP-over-vsock substrate the semgrep scanner uses) and awaits the
``AgentResult`` inline.

Delivery is split from the run trigger so the caller (``dispatch.submit``) returns
immediately: ``run_and_deliver`` is fired off as a detached task. It marks the run
COMPLETED/FAILED in the ledger, always marks the live-progress stream done (so the
Discord bot's stream loop terminates), and, when the run fronts a Discord thread,
enqueues the result to the Discord outbox for the leader's bot to post.

The progress path is unchanged: the guest streams goose's stdout to
``progressUrl`` (the monolith's own ``/internal/goosecracker/progress/{session}``
endpoint, reached through the fc-invoke egress funnel), the bot polls the
in-memory buffer keyed by the session id. This runner only marks that buffer done
at the end.
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx
from sqlmodel import Session

from app.db import get_engine
from goosecracker import threads, tiers

logger = logging.getLogger(__name__)

# The in-cluster fc-invoke daemon (shared with the semgrep_scan tool). Injected
# from Helm values; never hardcoded.
FC_INVOKE_URL = os.environ.get("FC_INVOKE_URL", "")

# The monolith's own in-cluster progress endpoint base, reached by the guest
# through the fc-invoke egress funnel. The runner appends "/{session}" so the
# guest's id-less {"chunk": ...} posts land in the right per-session buffer.
PROGRESS_URL_BASE = os.environ.get("GOOSECRACKER_PROGRESS_URL", "")

# A fast connect surfaces a down daemon quickly; a generous read budget lets a
# multi-turn goose run finish (cold Qwen runs take minutes).
_CONNECT_TIMEOUT = 5.0
_READ_TIMEOUT = 600.0

# Discord's hard message limit is 2000 chars; leave room for the prefix.
_MAX_DISCORD = 1800


def _progress_url(session: str) -> str:
    if not PROGRESS_URL_BASE:
        return ""
    return f"{PROGRESS_URL_BASE.rstrip('/')}/{session}"


def _truncate(text_body: str) -> str:
    if len(text_body) <= _MAX_DISCORD:
        return text_body
    return text_body[: _MAX_DISCORD - 1] + "…"


def _enqueue_sync(channel_id: str, content: str) -> None:
    """Open a session, enqueue a Discord outbox row, commit. Sync so the async
    runner hands it to a worker thread (a sync Session must not run on the event
    loop - semgrep no-sync-session-in-async-def)."""
    from chat.api import enqueue_message

    with Session(get_engine()) as session:
        enqueue_message(session, channel_id, content=content)
        session.commit()


async def _deliver(discord_thread: str, content: str) -> None:
    """Post a result/error into the run's Discord thread, if it fronts one."""
    if not discord_thread:
        return
    try:
        await asyncio.to_thread(_enqueue_sync, discord_thread, content)
    except Exception:
        logger.exception(
            "goosecracker: failed to enqueue result for %s", discord_thread
        )


def _mark_progress_done(session: str) -> None:
    """Mark the live-progress buffer done so the bot's stream loop terminates."""
    from chat.api import mark_goosecracker_progress_done

    mark_goosecracker_progress_done(session)


async def run_and_deliver(
    session: str,
    *,
    task: str,
    recipe: str,
    tier: str,
    git_mirror: str,
    git_ref: str,
    discord_thread: str,
) -> None:
    """POST the goose run to fc-invoke, then mark + deliver the result.

    Always marks the progress stream done (in a ``finally``) so the bot's stream
    loop ends whether the run succeeded, failed, or the daemon was unreachable.
    """
    try:
        if not FC_INVOKE_URL:
            raise RuntimeError("FC_INVOKE_URL is not configured")

        env = tiers.env_for_tier(tier)
        payload = {
            "recipe": recipe,
            "task": task,
            "session": session,
            "env": env,
            "progressUrl": _progress_url(session),
            "gitMirror": git_mirror,
            "gitRef": git_ref,
        }
        timeout = httpx.Timeout(_READ_TIMEOUT, connect=_CONNECT_TIMEOUT)
        url = f"{FC_INVOKE_URL}/invoke/agent/{session}"

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(
                f"fc-invoke returned HTTP {exc.response.status_code}: "
                f"{exc.response.text[:500]}"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"could not reach fc-invoke: {exc}") from exc

        status = data.get("status")
        if status == "ok":
            result = data.get("result", "") or "(no output)"
            # nosemgrep: no-session-in-to-thread  # `session` is the fc-invoke session-id string, not a SQLAlchemy Session
            await asyncio.to_thread(threads.mark_completed, session, result)
            await _deliver(discord_thread, _truncate(f"Artifact ready.\n\n{result}"))
        else:
            err = data.get("error", "") or "goose run failed with no detail"
            # nosemgrep: no-session-in-to-thread  # `session` is the fc-invoke session-id string, not a SQLAlchemy Session
            await asyncio.to_thread(threads.mark_failed, session, err)
            await _deliver(discord_thread, _truncate(f"Run failed: {err}"))
    except Exception as exc:  # noqa: BLE001 - any failure must mark + deliver
        logger.exception("goosecracker: run_and_deliver failed for %s", session)
        try:
            # nosemgrep: no-session-in-to-thread  # `session` is the fc-invoke session-id string, not a SQLAlchemy Session
            await asyncio.to_thread(threads.mark_failed, session, str(exc))
        except Exception:
            logger.exception("goosecracker: failed to mark run failed for %s", session)
        await _deliver(discord_thread, _truncate(f"Run failed: {exc}"))
    finally:
        _mark_progress_done(session)
