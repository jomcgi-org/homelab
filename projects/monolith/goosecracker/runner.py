"""Delivery, message composition, and session helpers for goosecracker.

Guest execution is intentionally not implemented here. The remaining helpers
format and deliver already-produced results, publish artifacts, and preserve
session snapshots for independent callers.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from sqlmodel import Session

from core.db import get_engine
from goosecracker import sessions

logger = logging.getLogger(__name__)

# Discord's hard message limit is 2000 chars; leave room for the prefix.
_MAX_DISCORD = 1800

# Sub-recipe bodies (query/plan/implement/research/artifact-*) are NOT baked
# into the guest image any more: the runner injects them into /injected-context
# fresh every turn, the same channel that already delivers the router. That is
# what lets a snapshot-resumed thread run CURRENT sub-recipe text instead of the
# frozen copy its rootfs was snapshotted with. The source YAMLs live in the
# guest recipe package and are shipped into this binary's runfiles as data (see
# `//projects/monolith:main` data -> `:recipe_yamls`); read cross-package via
# the same parents[] hop the recipe-catalog drift test uses.
# No .resolve(): the runfiles entry is a symlink, and resolving it would follow
# the file out to a content-addressed store where this relative parents[] hop no
# longer reaches the sibling firecracker/ package. The recipe-catalog drift test
# reads the same dir the same way (parents-relative, unresolved).
_GUEST_RECIPES_DIR = (
    Path(__file__).parents[2] / "firecracker/goosecracker/guest/recipes"
)


@lru_cache(maxsize=1)
def _subrecipe_bodies() -> dict[str, str]:
    """Every catalog sub-recipe body, keyed by the basename the router points at.

    Returns ``{"<id>.yaml": <body>}`` for each id in ``CATALOG``, ready to merge
    into ``injectedContext`` so the guest finds each body at
    ``/injected-context/<id>.yaml`` (the path ``render_router`` /
    ``render_fallback_router`` resolve ``delegate`` against). All ids are always
    injected, not just a plan's enabled subset, because an enabled sub-recipe can
    transitively delegate to another (plan and implement both delegate research).
    Cached: the bodies are immutable in the image, so read once.
    """
    from goosecracker.recipe_catalog import CATALOG

    return {
        f"{recipe_id}.yaml": (_GUEST_RECIPES_DIR / f"{recipe_id}.yaml").read_text(
            encoding="utf-8"
        )
        for recipe_id in CATALOG
    }


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


def _enqueue_whatsapp_sync(session_key: str, content: str) -> None:
    """Resolve the run's group JID and enqueue a whatsapp_outbox message (sync).

    The WhatsApp counterpart to ``_enqueue_sync``: the runner delivers by group
    JID (stored on the session row, since the sanitized session key cannot be
    reversed to a JID), so it resolves the JID then enqueues. A run whose row is
    gone (no JID) is skipped."""
    from chat.api import enqueue_whatsapp_message, whatsapp_group_jid_for_session

    group_jid = whatsapp_group_jid_for_session(session_key)
    if not group_jid:
        return
    enqueue_whatsapp_message(group_jid, content)


async def _deliver(
    discord_thread: str, content: str, provider: str = "discord"
) -> None:
    """Post a result/error into the run's channel, if it fronts one.

    ``provider`` selects the sink: "discord" (default) pages into the Discord
    thread's outbox; "whatsapp" enqueues to chat.whatsapp_outbox (the Go gateway
    sends). Long content is paged into several messages (Discord caps a message at
    2000 chars) rather than truncated, so the full answer reaches the channel.
    """
    if not discord_thread:
        return
    for page in _split_message(content):
        try:
            if provider == "whatsapp":
                await asyncio.to_thread(_enqueue_whatsapp_sync, discord_thread, page)
            else:
                await asyncio.to_thread(_enqueue_sync, discord_thread, page)
        except Exception:
            logger.exception(
                "goosecracker: failed to enqueue result for %s", discord_thread
            )


async def _settle_result(
    discord_thread: str, content: str, provider: str = "discord"
) -> None:
    """Deliver a run's terminal content to the right sink for its channel.

    Discord settles in place, overwriting the run's single live progress message
    (ADR 024). WhatsApp has no in-place edit for the result: its checklist is
    settled separately (``whatsapp_checklist_final``), so the result is a fresh
    paged send into chat.whatsapp_outbox.
    """
    if provider == "whatsapp":
        await _deliver(discord_thread, content, "whatsapp")
    else:
        await _settle(discord_thread, content)


def _enqueue_edit_sync(channel_id: str, message_id: str, content: str) -> None:
    """Open a session, enqueue a Discord outbox edit row, commit. Sync so the
    async runner hands it to a worker thread (a sync Session must not run on the
    event loop - semgrep no-sync-session-in-async-def)."""
    from chat.api import enqueue_edit

    with Session(get_engine()) as session:
        enqueue_edit(session, channel_id, message_id, content)
        session.commit()


async def _settle(discord_thread: str, content: str) -> None:
    """Deliver a run's terminal content into its single live message (ADR 024).

    Overwrites the run's live progress message in place via the durable,
    leader-safe outbox edit, so the checklist message becomes the result: one
    message, not a separate second post. A result too long for one Discord
    message settles the live message to its first page and posts the overflow as
    follow-ups, so the live message always ends on the real result (never a
    stranded checklist) and the full answer still reaches the thread. Falls back
    to a fresh paged post when there is no live message id (an MCP session, or a
    lost row) or the edit cannot be enqueued, so the result is never dropped.

    The live message id is consumed (read-and-cleared) so it is settled at most
    once: a later turn in the same run (the conversational drain posts no fresh
    live message) falls back to posting its own result rather than overwriting
    this turn's.
    """
    if not discord_thread:
        return
    msg_id = ""
    try:
        from chat.api import take_progress_message

        # nosemgrep: no-session-in-to-thread  # discord_thread is a str id, not a SQLAlchemy Session
        msg_id = await asyncio.to_thread(take_progress_message, discord_thread)
    except Exception:
        logger.exception(
            "goosecracker: failed to read live message id for %s", discord_thread
        )
    if msg_id:
        # Edit the live message to the first page (so it settles on the result,
        # not a stranded spinner) and post any overflow pages as follow-ups.
        pages = _split_message(content)
        try:
            await asyncio.to_thread(
                _enqueue_edit_sync, discord_thread, msg_id, pages[0]
            )
            for page in pages[1:]:
                await asyncio.to_thread(_enqueue_sync, discord_thread, page)
            return
        except Exception:
            logger.exception(
                "goosecracker: failed to settle edit for %s; posting instead",
                discord_thread,
            )
    await _deliver(discord_thread, content)


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


async def _agent_reply_message(
    session: str, summary: str, details: str, provider: str = "discord"
) -> str:
    """Compose the conversational reply for a coding-agent run.

    Rephrases goose's typed ``summary`` (+ optional ``details``) in the bot's own
    voice, grounded in the parent channel's context (recent messages + rolling
    summaries), so the reply reads like the bot talking to the channel rather than
    a raw tool dump. The result URL is appended by the caller, never by the model.

    ``provider`` selects the authoring model: WhatsApp (household) rephrases on
    DeepSeek V4 Flash, Discord stays on in-cluster Qwen (ADR 039, amended).

    Fail-open: the reply must always go out, so any missing channel id, model
    outage, or error yields the deterministic ``summary``/``details`` composition
    (exactly what this path posted before the concierge existed). Reaches the chat
    domain only through ``chat.api`` (import_boundaries_test).
    """
    deterministic = summary + (f"\n\n{details}" if details else "")
    try:
        from chat.api import (
            build_openrouter_caller,
            conversational_agent_reply,
            parent_channel_for_thread,
        )

        # nosemgrep: no-session-in-to-thread  # `session` is the fc-invoke session-id string, not a SQLAlchemy Session
        parent = await asyncio.to_thread(parent_channel_for_thread, session)
        if not parent:
            return deterministic
        # Household content is authored by DeepSeek; other tiers keep the default
        # in-cluster Qwen caller (llm_call=None leaves conversational_agent_reply
        # byte-identical to the Discord path).
        llm_call = build_openrouter_caller() if provider == "whatsapp" else None
        reply = (
            await conversational_agent_reply(
                parent, summary, details, llm_call=llm_call
            )
        ).strip()
        return reply or deterministic
    except Exception:
        logger.exception(
            "goosecracker: conversational reply failed for %s; using raw summary",
            session,
        )
        return deterministic


async def _delivery_message(session: str, data: dict, provider: str = "discord") -> str:
    """Build the channel message for a successful run.

    A run that produced artifact HTML (an agent run the router took down the
    artifact-build path) publishes it and posts a clean ``Artifact ready: <url>``
    plus the one-line summary, instead of the raw goose transcript. Any other run
    prefers the recipe's typed response (a JSON object from response.json_schema):
    summary, optional details, and a PR/issue url. It falls back to the legacy
    markdown summary, then goose's trailing narrative, so there is always a
    meaningful response. When the guest recorded a scratch ref (WS3), a
    ``recorded: refs/agents/<session>`` line is appended for all run types.
    """
    html = data.get("artifactHtml")
    if html:
        try:
            # nosemgrep: no-session-in-to-thread  # `session` is the fc-invoke session-id string, not a SQLAlchemy Session
            url = await asyncio.to_thread(_publish_artifact, session, html)
        except Exception:
            logger.exception("goosecracker: artifact publish failed for %s", session)
            return "Build finished, but publishing the artifact failed. Check the logs."
        # Summary comes from the router's typed JSON (a /agent run routed to the
        # artifact-build sub-recipe), falling back to the markdown goose-result.
        result_text = data.get("result", "") or ""
        structured = _parse_structured_result(result_text)
        summary = (
            str(structured.get("summary", "")).strip() if structured else ""
        ) or _extract_summary(result_text)
        msg = f"Artifact ready: {url}" + (f"\n\n{summary}" if summary else "")
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
            msg = await _agent_reply_message(session, summary, details, provider)
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
