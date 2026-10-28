"""Consent-gated chat-versus-session routing with fail-open telemetry.

compile() makes only that routing decision, never a submit_plan call. Each
enabled, granted attempt records one telemetry row from the routing response.
Session callers keep their raw prompt, selected repository and model.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from sqlmodel import Session

from core.db import get_engine
from chat import acl, orchestrator_client
from chat.models import OrchestratorBrief
from goosecracker.api import describe_repos

logger = logging.getLogger(__name__)

_BUNDLE_PATH = Path(__file__).with_name("orchestrator_bundle.md")

# Feature name for the ADR 029 consent grant that gates the orchestrator tier.
ORCHESTRATOR_FEATURE = "orchestrator"


class BriefParseError(Exception):
    """Raised when the model's output cannot be parsed into a valid verdict for
    the active route. The caller (:func:`compile`) turns this into a fail-open."""


@dataclass(frozen=True)
class Directive:
    """The active channel directive, referenced by version for cache stability."""

    version: int = 0
    text: str = ""


@dataclass
class SessionVerdict:
    """A session route with advisory repo metadata, never session input.

    brief_id links the routing telemetry to the thread created by the caller.
    Out-of-scope repos are replaced with the invoker's scope by compile().
    """

    repo: str = ""
    repo_paths: list[str] = field(default_factory=list)
    repo_replaced: bool = False
    brief_id: int | None = None


@dataclass
class ChatVerdict:
    """A conversational verdict carrying the model's reply guidance (ADR 036
    chat-route amendment). The Meta Spark concierge authors the reply using this
    as advisory context; ``redirect`` is optional (e.g. "this is really a repo
    task, offer to escalate")."""

    context: str
    direction: str
    redirect: str = ""


@dataclass
class FailOpen:
    """Degrade to today's direct-submit path. ``reason`` is logged and, when the
    model was actually called, recorded in the telemetry row's ``error``."""

    reason: str
    # Id of the telemetry row, when this fail-open wrote one (the ungranted /
    # disabled short-circuits write none, so it stays None). start_agent_flow
    # backfills its thread_id once the fallback thread exists (see link_thread).
    brief_id: int | None = None


@dataclass
class RequestContext:
    """Everything :func:`compile` needs for one escalation.

    ``allowed_scopes`` are the invoker's ADR 029 repo grants; ``invoker_scope``
    is the repo the command was invoked with (used to replace an out-of-scope
    brief repo). ``similar_messages`` (contextually similar past messages from
    this channel, retrieved by pgvector similarity) and ``channel_context`` are
    volatile grounding placed in the ``user`` message only.
    """

    request: str
    guild_id: str = ""
    channel_id: str = ""
    thread_id: str | None = None
    invoker_scope: str = ""
    allowed_scopes: frozenset[str] = frozenset()
    channel_context: str = ""
    similar_messages: list[str] = field(default_factory=list)
    directive: Directive = field(default_factory=Directive)


Verdict = SessionVerdict | ChatVerdict | FailOpen


def enabled() -> bool:
    """True when the orchestrator tier is configured (spec section 6).

    The chart only injects ``ORCHESTRATOR_MODEL`` when ``orchestrator.enabled``
    is set, so its presence is the enable signal. When false, :func:`compile`
    returns a fail-open without touching the client or the API key.
    """
    return bool(os.environ.get("ORCHESTRATOR_MODEL", "").strip())


def load_bundle() -> str:
    """Read the committed baked context bundle (the stable ``system`` prefix)."""
    return _BUNDLE_PATH.read_text(encoding="utf-8")


def assemble_prompt(
    bundle: str,
    directive: Directive,
    similar_messages: list[str],
    channel_context: str,
    request: str,
    repo_menu: str = "",
) -> tuple[str, str]:
    """Assemble the (system, user) messages in strict stability order (spec
    section 4).

    ``system`` = baked bundle + the versioned channel directive; it contains no
    timestamps, ids, or unsorted collections, so two consecutive escalations in
    the same channel produce byte-identical ``system`` messages and the
    provider's prefix cache hits. All volatile content (contextually similar
    past messages, channel context, the invoker-scoped repo menu, the request)
    goes in ``user``. ``repo_menu`` is per-invoker (their ADR 029 grants), so it
    rides here rather than in the cached system prefix. Byte-deterministic:
    identical inputs give identical bytes.
    """
    system = (
        bundle.rstrip("\n")
        + f"\n\n## Channel directive (version {directive.version})\n\n"
        + (directive.text.strip() or "(no channel directive set)")
        + "\n"
    )
    similar_block = (
        "\n".join(f"- {r}" for r in similar_messages) if similar_messages else "(none)"
    )
    user = (
        "## Contextually similar past messages in this channel\n\n"
        + similar_block
        + "\n\n## Channel context\n\n"
        + (channel_context.strip() or "(none)")
        + "\n\n## Available repos\n\n"
        + "When the request needs a repo checkout, set the goose route's `repo` "
        "to exactly one of these owner/repo ids, and leave it empty only for a "
        "repo-less artifact or build. Never name a repo that is not listed.\n\n"
        + (repo_menu.strip() or "(none)")
        + "\n\n## Request\n\n"
        + request.strip()
        + "\n"
    )
    return system, user


def _strip_fences(content: str) -> str:
    """Strip surrounding whitespace and an optional ```json ... ``` fence.

    The prompt asks for bare JSON, but models occasionally wrap it in a fenced
    block; unwrapping it here avoids a needless fail-open on otherwise valid
    output. Anything else is left untouched for ``json.loads`` to accept or
    reject.
    """
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        # Drop the opening fence line (```json / ```) and a trailing fence.
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text


def _parse_goose(data: dict, allowed_scopes: frozenset[str]) -> SessionVerdict:
    repo = data.get("repo")
    if not isinstance(repo, str):
        repo = ""
    paths = data.get("repo_paths")
    repo_paths = [str(p) for p in paths] if isinstance(paths, list) else []
    repo_replaced = bool(repo and repo not in allowed_scopes)
    return SessionVerdict(
        repo="" if repo_replaced else repo,
        repo_paths=repo_paths,
        repo_replaced=repo_replaced,
    )


def _parse_chat(data: dict) -> ChatVerdict:
    guidance = data.get("reply_guidance")
    if not isinstance(guidance, dict):
        raise BriefParseError("chat verdict missing 'reply_guidance' object")
    context = guidance.get("context")
    direction = guidance.get("direction")
    if not isinstance(context, str) or not context.strip():
        raise BriefParseError("chat reply_guidance missing 'context'")
    if not isinstance(direction, str) or not direction.strip():
        raise BriefParseError("chat reply_guidance missing 'direction'")
    redirect = guidance.get("redirect")
    redirect = redirect.strip() if isinstance(redirect, str) else ""
    return ChatVerdict(
        context=context.strip(), direction=direction.strip(), redirect=redirect
    )


def parse_brief(
    content: str, *, allowed_scopes: frozenset[str]
) -> SessionVerdict | ChatVerdict:
    """Parse the model's strict-JSON output, route-partitioned (spec section 3).

    ``route: goose`` validates and returns a :class:`SessionVerdict`; ``route: chat``
    validates the reply-guidance block and returns a :class:`ChatVerdict`. The
    non-active block may be absent, and unknown keys are tolerated; missing
    required keys for the active route raise :class:`BriefParseError` (which the
    caller turns into a fail-open). A repo outside ``allowed_scopes`` is
    discarded with ``SessionVerdict.repo_replaced`` set.
    """
    text = _strip_fences(content)
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise BriefParseError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise BriefParseError("top-level JSON is not an object")

    route = data.get("route")
    if route == "goose":
        return _parse_goose(data, allowed_scopes)
    if route == "chat":
        return _parse_chat(data)
    raise BriefParseError(f"unknown or missing route: {route!r}")


def _guidance_to_json(verdict: ChatVerdict) -> dict:
    """Serialize chat reply guidance for the telemetry ``brief_json`` column."""
    return {
        "context": verdict.context,
        "direction": verdict.direction,
        "redirect": verdict.redirect,
    }


def _record(
    ctx: RequestContext,
    route: str,
    model: str,
    brief_json: dict | None,
    latency_ms: int,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    cached_tokens: int | None,
    error: str | None,
) -> int | None:
    """Write exactly one telemetry row and return its id.

    The id lets :func:`compile` hand the row to the caller (on a verdict that
    opens a thread) so :func:`link_thread` can backfill ``thread_id`` once the
    thread exists. Best-effort: a write failure must never break the escalation
    flow and returns None."""
    try:
        with Session(get_engine()) as session:
            row = OrchestratorBrief(
                thread_id=ctx.thread_id,
                model=model,
                route=route,
                brief_json=brief_json,
                directive_version=ctx.directive.version,
                latency_ms=latency_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cached_tokens=cached_tokens,
                error=error,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return row.id
    except Exception:
        logger.exception("orchestrator: failed to write telemetry row")
        return None


def link_thread(brief_id: int, thread_id: str) -> None:
    """Backfill a telemetry row's ``thread_id`` once the session thread exists.

    The orchestrator runs BEFORE the session thread is created (deciding whether
    to open one), so :func:`_record` writes the row with a null ``thread_id``.
    Once ``start_agent_flow`` (chat/bot.py) has created the thread, it calls this
    with the row id carried on the verdict, joining the routing verdict to the
    agent session it produced. Best-effort and idempotent: a
    write failure must never break the run, and an already-linked row (non-null
    ``thread_id``) is left untouched."""
    try:
        with Session(get_engine()) as session:
            row = session.get(OrchestratorBrief, brief_id)
            if row is not None and row.thread_id is None:
                row.thread_id = thread_id
                session.commit()
    except Exception:
        logger.exception("orchestrator: failed to link thread to brief row")


async def compile(ctx: RequestContext) -> Verdict:
    """Run the orchestrator for one escalation and return a verdict.

    Order (spec sections 1-2):

    1. If the tier is disabled OR the channel lacks the ADR 029 consent grant,
       return :class:`FailOpen` WITHOUT calling the client and WITHOUT writing a
       row (no OpenRouter traffic, no key read on that path).
    2. Otherwise assemble the prompt and make the route-decision call. A timeout
       / HTTP error / unparseable output all fail open, each writing one
       ``failopen`` telemetry row.
    3. On the chat route, write one ``chat`` row and return the
       :class:`ChatVerdict`.
    4. On the goose route, apply the ADR 029 repo replacement, write one
       plan-free row and return a SessionVerdict carrying its row id.
    """
    if not enabled():
        return FailOpen("orchestrator disabled")
    granted = await asyncio.to_thread(
        acl.is_granted, ctx.guild_id, "", ORCHESTRATOR_FEATURE, ctx.channel_id
    )
    if not granted:
        return FailOpen("channel not granted")

    model = os.environ.get("ORCHESTRATOR_MODEL", "")
    system, user = assemble_prompt(
        load_bundle(),
        ctx.directive,
        ctx.similar_messages,
        ctx.channel_context,
        ctx.request,
        describe_repos(ctx.allowed_scopes),
    )

    try:
        resp = await orchestrator_client.call(system, user)
    except orchestrator_client.OrchestratorUnavailable as exc:
        reason = f"orchestrator unavailable: {exc}"
        logger.warning("orchestrator: failing open, %s", reason)
        brief_id = await asyncio.to_thread(
            _record, ctx, "failopen", model, None, 0, None, None, None, reason
        )
        return FailOpen(reason, brief_id=brief_id)

    try:
        verdict = parse_brief(resp.content, allowed_scopes=ctx.allowed_scopes)
    except BriefParseError as exc:
        reason = f"unparseable brief: {exc}"
        logger.warning("orchestrator: failing open, %s", reason)
        brief_id = await asyncio.to_thread(
            _record,
            ctx,
            "failopen",
            model,
            None,
            resp.latency_ms,
            resp.prompt_tokens,
            resp.completion_tokens,
            resp.cached_tokens,
            reason,
        )
        return FailOpen(reason, brief_id=brief_id)

    if isinstance(verdict, SessionVerdict):
        if verdict.repo_replaced:
            logger.warning(
                "orchestrator: brief named a repo outside invoker scopes; "
                "using invoker scope %r instead",
                ctx.invoker_scope,
            )
            verdict.repo = ctx.invoker_scope

        brief_id = await asyncio.to_thread(
            _record,
            ctx,
            "goose",
            model,
            {
                "repo": verdict.repo,
                "repo_paths": verdict.repo_paths,
                "repo_replaced": verdict.repo_replaced,
            },
            resp.latency_ms,
            resp.prompt_tokens,
            resp.completion_tokens,
            resp.cached_tokens,
            None,
        )
        verdict.brief_id = brief_id
        return verdict

    await asyncio.to_thread(
        _record,
        ctx,
        "chat",
        model,
        _guidance_to_json(verdict),
        resp.latency_ms,
        resp.prompt_tokens,
        resp.completion_tokens,
        resp.cached_tokens,
        None,
    )
    return verdict
