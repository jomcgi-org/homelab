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
import time

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

# fc-invoke is a single replica with a ~90s cold start (it builds base rootfs
# and warms a snapshot before its HTTP shim listens). A chart bump rolls it, and
# any run landing in that gap gets a gateway 502/503/504 or a refused
# connection: the run never started, so re-POSTing the same session is safe.
# Ride that out with a small exponential backoff bounded by _RETRY_DEADLINE of
# wall time; a genuine outage still surfaces within ~2 minutes. A read timeout
# (the run was accepted and goose ran long) or any other status is terminal:
# retrying there would spawn a duplicate run.
_RETRY_STATUSES = frozenset({502, 503, 504})
_RETRY_DEADLINE = 120.0  # total wall time across all attempts, seconds
_RETRY_BASE = 2.0  # first backoff sleep, doubled each attempt
_RETRY_MAX_SLEEP = 20.0  # cap on any single backoff sleep

# Discord's hard message limit is 2000 chars; leave room for the prefix.
_MAX_DISCORD = 1800


def _progress_url(session: str) -> str:
    if not PROGRESS_URL_BASE:
        return ""
    return f"{PROGRESS_URL_BASE.rstrip('/')}/{session}"


# The monolith's own in-cluster steering endpoint base (ADR 035 phase 2). The
# runner injects "<this>/<token>" into the guest env as
# GOOSECRACKER_STEERING_URL so the recipe can poll it at stage boundaries for
# mid-run adjustments posted to the thread. Keyed on the unguessable per-session
# token (not the session/thread id) so a compromised guest cannot fetch another
# thread's steering by guessing its Discord thread snowflake.
STEERING_URL_BASE = os.environ.get("GOOSECRACKER_STEERING_URL", "")


def _steering_url(token: str) -> str:
    if not STEERING_URL_BASE or not token:
        return ""
    return f"{STEERING_URL_BASE.rstrip('/')}/{token}"


def _effective_mirror_ref(
    git_mirror: str, git_ref: str, repo: str = ""
) -> tuple[str, str]:
    """Return the (mirror URL, ref) pair for a goose run, applying defaults.

    When the caller does not specify a mirror but names a ``repo``, defaults to
    ``<GOOSECRACKER_GIT_MIRROR>/<repo>`` (the in-cluster mirror). ``repo`` is the
    GitHub-convention ``owner/repo`` name registered on the git-mirror; the slash
    simply nests the git:// path. When no ref is specified, defaults to ``main``.
    An explicit ``git_mirror`` is passed through unchanged and always wins, with
    ``repo`` ignored in that case.

    There is NO implicit default repo: an empty ``repo`` (and no explicit mirror)
    yields an empty mirror, which makes the handler skip the clone entirely. That
    is the repo-less path, e.g. a ``/agent`` artifact build that needs no checkout
    (ADR 029). An empty mirror is also returned when GOOSECRACKER_GIT_MIRROR is
    unset (no mirror configured in this environment).
    """
    effective_mirror = git_mirror
    if not effective_mirror and GOOSECRACKER_GIT_MIRROR and repo:
        effective_mirror = GOOSECRACKER_GIT_MIRROR.rstrip("/") + "/" + repo
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


def _publish_artifact(session: str, html: str) -> str:
    """Publish the built artifact HTML to S3 and return its live URL (ADR 024).

    Reuses the /internal/artifact publish path so the S3 write + URL/version logic
    lives in one place. Publishes under the thread's random capability id (not the
    thread id, via chat.api) so the URL is unguessable but stable across
    re-publishes (ADR 024 amendment).
    """
    from artifact.router import PublishRequest, publish_artifact
    from chat.api import artifact_id_for_thread

    artifact_id = artifact_id_for_thread(session)
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


async def _agent_reply_message(session: str, summary: str, details: str) -> str:
    """Compose the conversational reply for a coding-agent run.

    Rephrases goose's typed ``summary`` (+ optional ``details``) in the bot's own
    voice, grounded in the parent channel's context (recent messages + rolling
    summaries), so the reply reads like the bot talking to the channel rather than
    a raw tool dump. The result URL is appended by the caller, never by the model.

    Fail-open: the reply must always go out, so any missing channel id, model
    outage, or error yields the deterministic ``summary``/``details`` composition
    (exactly what this path posted before the concierge existed). Reaches the chat
    domain only through ``chat.api`` (import_boundaries_test).
    """
    deterministic = summary + (f"\n\n{details}" if details else "")
    try:
        from chat.api import conversational_agent_reply, parent_channel_for_thread

        # nosemgrep: no-session-in-to-thread  # `session` is the fc-invoke session-id string, not a SQLAlchemy Session
        parent = await asyncio.to_thread(parent_channel_for_thread, session)
        if not parent:
            return deterministic
        reply = (await conversational_agent_reply(parent, summary, details)).strip()
        return reply or deterministic
    except Exception:
        logger.exception(
            "goosecracker: conversational reply failed for %s; using raw summary",
            session,
        )
        return deterministic


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
        # Summary can come from the artifact recipe's markdown goose-result
        # (/artifact command) or the router's typed JSON (a /agent run routed to
        # the artifact sub-recipe). Prefer the structured summary, fall back to
        # the markdown one.
        result_text = data.get("result", "") or ""
        structured = _parse_structured_result(result_text)
        summary = (
            str(structured.get("summary", "")).strip() if structured else ""
        ) or _extract_summary(result_text)
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
            summary = str(structured["summary"]).strip()
            details = str(structured.get("details", "") or "").strip()
            # Rephrase the typed summary conversationally (channel-scoped context),
            # then append the URL deterministically so it can never be mangled.
            msg = await _agent_reply_message(session, summary, details)
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


async def _post_agent_run(url: str, payload: dict, on_retry) -> dict:
    """POST a goose run to fc-invoke, retrying transient failures with backoff.

    Returns the parsed JSON body on the first success. Retries only failures
    where the run never started (a gateway 502/503/504 from a rollout gap, or a
    connect/transport error from a mid-restart daemon), sleeping with a small
    exponential backoff bounded by _RETRY_DEADLINE of wall time and calling
    ``on_retry(attempt, wait, reason)`` before each sleep so the caller can tell
    the user. A read timeout (the run was accepted and goose ran long) or a
    non-transient status re-raises a RuntimeError with the same message shape as
    before, so the caller's existing failure path is unchanged.
    """
    timeout = httpx.Timeout(_READ_TIMEOUT, connect=_CONNECT_TIMEOUT)
    deadline = time.monotonic() + _RETRY_DEADLINE
    attempt = 0
    while True:
        attempt += 1
        transient = False
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            transient = status in _RETRY_STATUSES
            reason = f"HTTP {status}"
            err = RuntimeError(
                f"fc-invoke returned HTTP {status}: {exc.response.text[:500]}"
            )
            cause = exc
        except httpx.ReadTimeout as exc:
            # The run was accepted and goose ran past the read budget; retrying
            # would spawn a duplicate. Terminal.
            raise RuntimeError(f"could not reach fc-invoke: {exc}") from exc
        except httpx.HTTPError as exc:
            # Connect/transport error: the daemon is mid-restart and nothing
            # started, so a re-POST of the same session is safe.
            transient = True
            reason = "connection failed"
            err = RuntimeError(f"could not reach fc-invoke: {exc}")
            cause = exc

        wait = min(_RETRY_BASE * 2 ** (attempt - 1), _RETRY_MAX_SLEEP)
        if not transient or time.monotonic() + wait >= deadline:
            raise err from cause
        on_retry(attempt, wait, reason)
        await asyncio.sleep(wait)


async def _run_one_turn(
    session: str,
    *,
    task: str,
    recipe: str,
    tier: str,
    repo: str = "",
    git_mirror: str,
    git_ref: str,
    discord_thread: str,
) -> bool:
    """POST one goose turn to fc-invoke, then mark + deliver the result.

    Returns True when the run succeeded, False on any failure. Always marks the
    progress stream done (in a ``finally``) so the bot's stream loop ends whether
    the run succeeded, failed, or the daemon was unreachable. The conversational
    drain/next-turn loop lives in ``run_and_deliver``, which calls this per turn.
    """
    ok = False
    # Reset the live-progress buffer for this session before the turn starts, so a
    # stale done=True left by a prior turn cannot poison this one. A runner-driven
    # turn (a reclaimed or drained await-loop turn) marks the buffer done with no
    # bot stream to consume+clear it, and the next bot-streamed turn would then
    # read that stale done and render "Done in 0:01" instead of live progress. The
    # bot's stream sleeps before its first poll, so clearing at dispatch wins the
    # race. Reached through chat.api so goosecracker never imports chat internals.
    from chat.api import reset_goosecracker_progress

    reset_goosecracker_progress(session)
    try:
        if not FC_INVOKE_URL:
            raise RuntimeError("FC_INVOKE_URL is not configured")

        env = tiers.env_for_tier(tier)
        # ADR 035 phase 2 (hardening): inject a steering URL keyed on an
        # unguessable per-session token, not the thread/session id, into a copy
        # of the tier env so the recipe can poll it without mutating the dict
        # env_for_tier returned. A compromised guest that reads this env only
        # learns its own token, which can never resolve another thread's
        # steering. Reached through chat.api's boundary the same as the other
        # chat.* calls in this module (import_boundaries_test).
        from chat.api import ensure_steering_token

        # nosemgrep: no-session-in-to-thread  # `session` is the fc-invoke session-id string, not a SQLAlchemy Session
        steering_token = await asyncio.to_thread(ensure_steering_token, session)
        steering_url = _steering_url(steering_token) if steering_token else ""
        if steering_url:
            env = {**env, "GOOSECRACKER_STEERING_URL": steering_url}
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
        url = f"{FC_INVOKE_URL}/invoke/agent/{session}"

        def _notify_retry(attempt: int, wait: float, reason: str) -> None:
            # Surface the retry on the live-progress message the bot is already
            # editing (a fast in-memory buffer write, no DB), so the owner sees
            # the run is waiting on fc-invoke rather than a bare "Thinking".
            from chat.api import set_goosecracker_progress_notice

            logger.info(
                "goosecracker: transient fc-invoke failure for %s (%s); "
                "retry %d in %.0fs",
                session,
                reason,
                attempt,
                wait,
            )
            set_goosecracker_progress_notice(
                session,
                f"⏳ fc-invoke busy ({reason}); retrying "
                f"(attempt {attempt}, waiting {wait:.0f}s)…",
            )

        data = await _post_agent_run(url, payload, _notify_retry)

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
            ok = True
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
    return ok


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
    """Run a conversational agent thread to completion, one turn at a time.

    Runs the given turn, then for agent threads drains any replies that arrived
    while it ran and runs them as the next turn, looping until the queue is empty.
    Each turn is ``await``-ed in place (never ``create_task``-ed): ``submit``
    dispatches the first turn on a throwaway daemon thread via ``asyncio.run``,
    which tears its loop down the instant the coroutine returns, so a
    ``create_task``-ed next turn would be cancelled before it ran (the "queued
    reply never comes back" bug). Awaiting keeps the whole chain inside that one
    live loop. Serial-by-design: one turn per thread at a time.

    Reaction lifecycle: each turn flips its queued messages ⏳→👀 at the start
    (``mark_inflight_running``) and 👀→✅/❌ at the end (``ack_inflight``), so a
    queued reply shows honest state on the user's own message with no text spam.
    """
    is_agent_thread = recipe == "agent" and bool(discord_thread)
    current_task = task
    while True:
        if is_agent_thread:
            # The queue/reaction bookkeeping lives in the chat domain; reach it
            # through chat.api so goosecracker never imports chat internals
            # (import_boundaries_test).
            from chat.api import mark_inflight_running

            # nosemgrep: no-session-in-to-thread  # discord_thread is a str id, not a SQLAlchemy Session
            await asyncio.to_thread(mark_inflight_running, discord_thread)

        ok = await _run_one_turn(
            session,
            task=current_task,
            recipe=recipe,
            tier=tier,
            repo=repo,
            git_mirror=git_mirror,
            git_ref=git_ref,
            discord_thread=discord_thread,
        )

        if not is_agent_thread:
            return

        from chat.api import ack_inflight, drain_agent_queue, force_idle_thread

        # Post-turn bookkeeping. Guard it: if a transient DB error escaped here the
        # loop would die with running=True and the thread would wedge for the full
        # stale timeout. On failure, best-effort idle the row (CAS on ownership) so
        # the next reply can start a fresh turn, then stop this chain.
        try:
            # Resolve this turn's queued messages (👀 → ✅/❌) before draining the
            # next batch, so their reactions reflect this turn's outcome.
            # nosemgrep: no-session-in-to-thread  # discord_thread is a str id, not a SQLAlchemy Session
            await asyncio.to_thread(ack_inflight, discord_thread, ok)
            # nosemgrep: no-session-in-to-thread  # discord_thread is a str id, not a SQLAlchemy Session
            drained = await asyncio.to_thread(drain_agent_queue, discord_thread)
        except Exception:
            logger.exception(
                "goosecracker: post-turn bookkeeping failed for %s; idling thread",
                discord_thread,
            )
            # nosemgrep: no-session-in-to-thread  # discord_thread is a str id, not a SQLAlchemy Session
            await asyncio.to_thread(force_idle_thread, discord_thread)
            return
        if drained is None:
            # Queue empty (running=False set by drain) or this turn was re-owned by
            # another process after a leadership handover: either way, stop.
            return
        next_task, _ack_ids = drained
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
        current_task = next_task
