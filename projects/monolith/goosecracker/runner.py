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
import base64
import json
import logging
import os
import re
import secrets

import httpx
from sqlmodel import Session

from app.db import get_engine
from goosecracker import sessions, threads, tiers

logger = logging.getLogger(__name__)

# The in-cluster fc-invoke daemon (shared with the semgrep_scan tool). Injected
# from Helm values; never hardcoded.
FC_INVOKE_URL = os.environ.get("FC_INVOKE_URL", "")

# The monolith's own in-cluster progress endpoint base, reached by the guest
# through the fc-invoke egress funnel. The runner appends "/{session}" so the
# guest's id-less {"chunk": ...} posts land in the right per-session buffer.
PROGRESS_URL_BASE = os.environ.get("GOOSECRACKER_PROGRESS_URL", "")

# Base URL of the in-cluster git mirror. Injected from Helm values
# (GOOSECRACKER_GIT_MIRROR); never hardcoded here (semgrep no-hardcoded-k8s-service-url).
# When set, the runner defaults every agent run to clone from <mirror>/homelab
# unless the caller specifies git_mirror explicitly.
GOOSECRACKER_GIT_MIRROR = os.environ.get("GOOSECRACKER_GIT_MIRROR", "")

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


def _effective_mirror_ref(
    git_mirror: str, git_ref: str, repo: str = ""
) -> tuple[str, str]:
    """Return the (mirror URL, ref) pair for a goose run, applying defaults.

    When the caller does not specify a mirror, defaults to
    ``<GOOSECRACKER_GIT_MIRROR>/<repo or "homelab">`` (the in-cluster mirror).
    When no ref is specified, defaults to ``main``. Both values are passed
    through unchanged when explicitly supplied by the caller, so an override
    always wins.

    The ``repo`` param selects which repository under the mirror base to clone;
    it defaults to ``"homelab"`` when empty. Explicit ``git_mirror`` takes
    precedence and ``repo`` is ignored in that case.

    Returns ("", "main") when GOOSECRACKER_GIT_MIRROR is also unset, which
    makes the handler skip the clone step entirely (no mirror configured).
    """
    effective_mirror = git_mirror
    if not effective_mirror and GOOSECRACKER_GIT_MIRROR:
        effective_mirror = (
            GOOSECRACKER_GIT_MIRROR.rstrip("/") + "/" + (repo or "homelab")
        )
    effective_ref = git_ref or "main"
    return effective_mirror, effective_ref


# Long output is split into at most this many Discord messages; beyond it the
# final page is truncated, so a runaway dump cannot flood the thread.
_MAX_CHUNKS = 6


def _split_message(content: str) -> list[str]:
    """Split content into Discord-sized (<=_MAX_DISCORD) chunks so a long result
    posts as several messages instead of being cut off.

    Splits on line boundaries where possible; a single line longer than the limit
    is hard-split. Caps the number of pages at _MAX_CHUNKS, marking the last page
    truncated if the content still overflows.
    """
    content = content.rstrip()
    if not content:
        return ["(no output)"]
    chunks: list[str] = []
    cur = ""
    for line in content.split("\n"):
        while len(line) > _MAX_DISCORD:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:_MAX_DISCORD])
            line = line[_MAX_DISCORD:]
        candidate = line if not cur else cur + "\n" + line
        if len(candidate) > _MAX_DISCORD:
            chunks.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    if len(chunks) > _MAX_CHUNKS:
        chunks = chunks[:_MAX_CHUNKS]
        chunks[-1] = chunks[-1][: _MAX_DISCORD - 20].rstrip() + "\n… (truncated)"
    return chunks


def _enqueue_sync(channel_id: str, content: str) -> None:
    """Open a session, enqueue a Discord outbox row, commit. Sync so the async
    runner hands it to a worker thread (a sync Session must not run on the event
    loop - semgrep no-sync-session-in-async-def)."""
    from chat.api import enqueue_message

    with Session(get_engine()) as session:
        enqueue_message(session, channel_id, content=content)
        session.commit()


async def _deliver(discord_thread: str, content: str) -> None:
    """Post a result/error into the run's Discord thread, if it fronts one.

    Long content is paged into several messages (Discord caps a message at 2000
    chars) rather than truncated, so the full answer reaches the thread.
    """
    if not discord_thread:
        return
    for page in _split_message(content):
        try:
            await asyncio.to_thread(_enqueue_sync, discord_thread, page)
        except Exception:
            logger.exception(
                "goosecracker: failed to enqueue result for %s", discord_thread
            )


def _mark_progress_done(session: str) -> None:
    """Mark the live-progress buffer done so the bot's stream loop terminates."""
    from chat.api import mark_goosecracker_progress_done

    mark_goosecracker_progress_done(session)


def _artifact_id_for_thread(thread_id: str) -> str:
    """Return the thread's unguessable capability artifact id, assigning one on
    first use (ADR 024 amendment).

    Random per thread and stored on the session row so a re-publish reuses it and
    the live page hot-reloads at a stable URL, while the URL is NOT the enumerable
    Discord thread id, so it is not publicly discoverable but is safe to share by
    link. Falls back to a fresh random id if the thread has no session row.
    """
    from chat.models import GoosecrackerSession

    with Session(get_engine()) as session:
        row = session.get(GoosecrackerSession, thread_id)
        if row is None:
            return secrets.token_urlsafe(9)
        if not row.artifact_id:
            row.artifact_id = secrets.token_urlsafe(9)
            session.add(row)
            session.commit()
        return row.artifact_id


def _publish_artifact(session: str, html: str) -> str:
    """Publish the built artifact HTML to S3 and return its live URL (ADR 024).

    Reuses the /internal/artifact publish path so the S3 write + URL/version logic
    lives in one place. Publishes under the thread's random capability id (not the
    thread id) so the URL is unguessable but stable across re-publishes.
    """
    from artifact.router import PublishRequest, publish_artifact

    artifact_id = _artifact_id_for_thread(session)
    return publish_artifact(PublishRequest(html=html, id=artifact_id)).url


def _extract_summary(result: str) -> str:
    """Pull the recipe's ``goose-result`` summary line out of goose's output.

    The artifact recipe ends with a ```goose-result``` block carrying
    ``summary: <what it built>``; surface that one line rather than the raw
    transcript. Empty when absent.
    """
    m = re.search(r"^\s*summary:\s*(.+)$", result, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _extract_result_field(result: str, key: str) -> str:
    """Pull a ``<key>: <value>`` line out of the recipe's ``goose-result`` block.

    Used to surface the ``url`` (a PR/issue the agent opened) alongside the
    summary. Empty when the key is absent. The recipe's placeholder text (e.g.
    ``<artifact URL, if any>``) is left for the caller to filter (it is not a URL).
    """
    m = re.search(rf"^\s*{re.escape(key)}:\s*(.+)$", result, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _parse_structured_result(result: str) -> dict | None:
    """Parse the response-schema JSON goose emits as its final output.

    A recipe with a ``response.json_schema`` (the agent recipe) makes goose print
    a JSON object as the LAST line of stdout. Scan from the end for that trailing
    brace-delimited line and parse it. Returns the dict, or None when there is no
    valid trailing JSON object (e.g. a model that did not honor the schema), so
    the caller can fall back to the markdown summary or the narrative.
    """
    for line in reversed(result.strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
            except ValueError:
                return None
            return obj if isinstance(obj, dict) else None
        return None  # the last non-empty line is not a JSON object
    return None


# A clean answer never contains goose's terminal chrome: the startup banner text,
# the tool-call bullet (``▸``), or the box-drawing separators between tool blocks.
# If a message we are about to deliver still carries any of these, extraction
# failed and we are about to ship the raw transcript, which is noise and can leak
# the contents of files goose read (including this module's own source). Suppress
# it in favor of a clear miss instead.
_TRANSCRIPT_CHROME = ("goose is ready", "▸", "─")

_NO_CLEAN_ANSWER = (
    "I couldn't produce a clean answer for that one. Try rephrasing or narrowing "
    "the question."
)


def _looks_like_transcript(text: str) -> bool:
    """True when text still carries goose terminal chrome (see _TRANSCRIPT_CHROME)."""
    return any(marker in text for marker in _TRANSCRIPT_CHROME)


def _agent_narrative(result: str) -> str:
    """Extract goose's final narrative answer from a transcript that has no
    ``goose-result`` block (e.g. a question rather than a coding action).

    Goose's answer is what it writes after the startup banner, so strip the
    ``...goose is ready`` preamble and return the body. Anchor on the FIRST
    occurrence of the marker (the real startup banner prints it once): the string
    recurs wherever goose read a file that mentions it, including this module's own
    source, so ``rfind`` would slice the body mid-file. The caller guards the
    result with _looks_like_transcript, so a body that is still raw transcript
    (goose ran tools but never wrote a clean answer) is dropped rather than shipped.
    """
    marker = "goose is ready"
    idx = result.find(marker)
    body = (result[idx + len(marker) :] if idx != -1 else result).strip()
    return body or "(no output)"


async def _delivery_message(session: str, recipe: str, data: dict) -> str:
    """Build the Discord message for a successful run.

    Artifact runs publish the built HTML and post a clean ``Artifact ready: <url>``
    (plus the recipe's one-line summary) instead of the raw goose transcript. A run
    that was meant to build an artifact but produced none gets a clear miss message.
    Any other run (e.g. the coding ``agent``) prefers the recipe's typed response
    (a JSON object from response.json_schema): summary, optional details, and a
    PR/issue url. It falls back to the legacy markdown summary, then goose's
    trailing narrative, so there is always a meaningful response. When the guest
    recorded a scratch ref (WS3), a ``recorded: refs/agents/<session>`` line is
    appended for all run types.
    """
    html = data.get("artifactHtml")
    if html:
        try:
            # nosemgrep: no-session-in-to-thread  # `session` is the fc-invoke session-id string, not a SQLAlchemy Session
            url = await asyncio.to_thread(_publish_artifact, session, html)
        except Exception:
            logger.exception("goosecracker: artifact publish failed for %s", session)
            return "Build finished, but publishing the artifact failed. Check the logs."
        summary = _extract_summary(data.get("result", "") or "")
        msg = f"Artifact ready: {url}" + (f"\n\n{summary}" if summary else "")
    elif recipe == "artifact":
        msg = "Build finished but produced no artifact. Try rephrasing the request."
    else:
        # Coding agent (and any non-artifact recipe). Prefer the typed response
        # (the agent recipe's response.json_schema makes goose emit a JSON object
        # as its final line): post its summary, plus optional details and a real
        # url. Fall back to the legacy markdown goose-result summary, then to
        # goose's trailing narrative, so there is ALWAYS a meaningful response.
        result = data.get("result", "") or ""
        structured = _parse_structured_result(result)
        if structured and str(structured.get("summary", "")).strip():
            msg = str(structured["summary"]).strip()
            details = str(structured.get("details", "") or "").strip()
            if details:
                msg += f"\n\n{details}"
            url = str(structured.get("url", "") or "").strip()
            if url.startswith("http"):
                msg += f"\n{url}"
        else:
            summary = _extract_summary(result)
            if summary:
                msg = summary
                url = _extract_result_field(result, "url")
                if url.startswith("http"):
                    msg += f"\n{url}"
            else:
                # goose's answer is its trailing narrative (post that, not the head
                # of the transcript, which is the recipe banner + tool calls). If
                # what's left still looks like a raw transcript, extraction failed
                # (goose ran tools but never emitted structured output or a clean
                # answer, e.g. a truncated run): ship a miss, never the transcript.
                narrative = _agent_narrative(result)
                if _looks_like_transcript(narrative):
                    logger.warning(
                        "goosecracker: no clean answer for %s; suppressing raw transcript",
                        session,
                    )
                    msg = _NO_CLEAN_ANSWER
                else:
                    msg = narrative

    recorded_ref = data.get("recordedRef")
    if recorded_ref:
        msg += f"\nrecorded: {recorded_ref}"
    return msg


async def _persist_session_db(session: str, session_db_b64: str | None) -> None:
    """Persist the guest's returned sessions.db blob for a later resume.

    Best-effort (ADR 026 Phase 2): decode the base64 blob the guest exported and
    store it against the session. Any failure is logged and swallowed, because a
    failed persist only costs the next reply a cold rebuild, never the current run.
    """
    if not session_db_b64:
        return
    try:
        blob = base64.b64decode(session_db_b64)
        # nosemgrep: no-session-in-to-thread  # `session` is the fc-invoke session-id string, not a SQLAlchemy Session
        await asyncio.to_thread(sessions.save, session, blob)
    except Exception:
        logger.exception("goosecracker: failed to persist session db for %s", session)


async def run_and_deliver(
    session: str,
    *,
    task: str,
    recipe: str,
    tier: str,
    repo: str = "",
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
        # ADR 026 Phase 2: restore the thread's persisted goose session so this run
        # resumes the prior conversation (Model A) instead of cold-rebuilding
        # (Model B). Resume is derived from the blob's presence, not a stored flag:
        # a stored db means resume, its absence (first run) means cold. The guest
        # falls back to cold if the db fails to hydrate/resume.
        # nosemgrep: no-session-in-to-thread  # `session` is the fc-invoke session-id string, not a SQLAlchemy Session
        session_db = await asyncio.to_thread(sessions.load, session)
        # WS2 - Hydration: default the mirror and ref when the caller did not
        # specify them. The mirror is read from GOOSECRACKER_GIT_MIRROR, injected
        # via Helm values. An empty effective_mirror means no clone (no mirror
        # configured in this environment).
        effective_mirror, effective_ref = _effective_mirror_ref(
            git_mirror, git_ref, repo
        )
        payload = {
            "recipe": recipe,
            "task": task,
            "session": session,
            "env": env,
            "progressUrl": _progress_url(session),
            "gitMirror": effective_mirror,
            "gitRef": effective_ref,
            "resume": session_db is not None,
            "sessionDb": base64.b64encode(session_db).decode() if session_db else "",
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
            # Persist the updated goose session so the next reply resumes it
            # (ADR 026 Phase 2). Best-effort: a persist failure must not fail the
            # run (the next reply just cold-rebuilds).
            await _persist_session_db(session, data.get("sessionDb"))
            # nosemgrep: no-session-in-to-thread  # `session` is the fc-invoke session-id string, not a SQLAlchemy Session
            await asyncio.to_thread(threads.mark_completed, session, result)
            await _deliver(
                discord_thread, await _delivery_message(session, recipe, data)
            )
        else:
            err = data.get("error", "") or "goose run failed with no detail"
            # nosemgrep: no-session-in-to-thread  # `session` is the fc-invoke session-id string, not a SQLAlchemy Session
            await asyncio.to_thread(threads.mark_failed, session, err)
            await _deliver(discord_thread, f"Run failed: {err}")
    except Exception as exc:  # noqa: BLE001 - any failure must mark + deliver
        logger.exception("goosecracker: run_and_deliver failed for %s", session)
        try:
            # nosemgrep: no-session-in-to-thread  # `session` is the fc-invoke session-id string, not a SQLAlchemy Session
            await asyncio.to_thread(threads.mark_failed, session, str(exc))
        except Exception:
            logger.exception("goosecracker: failed to mark run failed for %s", session)
        await _deliver(discord_thread, f"Run failed: {exc}")
    finally:
        _mark_progress_done(session)

    # For agent runs fronting a Discord thread: drain any replies that arrived
    # while this turn was running, then dispatch them as the next turn.
    # This also clears running=False on the failed path so a crashed turn does
    # not wedge the thread. Runs after finally: so the progress stream is already
    # marked done before the next turn potentially starts.
    if recipe == "agent" and discord_thread:
        # The drain (DB work on chat.goosecracker_sessions) lives in the chat
        # domain; reach it through chat.api so goosecracker never imports chat
        # internals (import_boundaries_test).
        from chat.api import drain_agent_queue

        # nosemgrep: no-session-in-to-thread  # discord_thread is a str id, not a SQLAlchemy Session
        next_task = await asyncio.to_thread(drain_agent_queue, discord_thread)
        if next_task is not None:
            # Update the run ledger for the next task (mirrors dispatch.submit).
            # nosemgrep: no-session-in-to-thread  # threads.upsert_run opens its own Session
            await asyncio.to_thread(
                threads.upsert_run,
                session,
                recipe=recipe,
                tier=tier,
                task=next_task,
                discord_thread=discord_thread,
            )
            # Schedule the next turn as a detached task on the current event loop.
            # No import of dispatch (circular: dispatch imports runner); use
            # create_task directly since we are already inside an async context.
            asyncio.create_task(
                run_and_deliver(
                    session,
                    task=next_task,
                    recipe=recipe,
                    tier=tier,
                    repo=repo,
                    git_mirror=git_mirror,
                    git_ref=git_ref,
                    discord_thread=discord_thread,
                )
            )
