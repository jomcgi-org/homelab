"""ADR 036 orchestrator brief compiler: consent gate, prompt assembly, parse,
and fail-open telemetry.

This is the host-side tier that runs on an escalation candidate before any
Firecracker microVM boots. It assembles a deterministic, cache-friendly prompt
(baked bundle + versioned channel directive as ``system``; the volatile context
as ``user``), calls the OpenRouter client once, and turns the reply into one of:

- ``Brief``       - a grounded goose task brief (recipe, paths, hints,
                    constraints, done criteria, stage plan).
- ``ChatVerdict`` - the request is conversational; carries the model's reply
                    guidance (context, direction, optional redirect) for the
                    local-Qwen concierge to author the actual reply.
- ``FailOpen``    - degrade to today's direct-submit path (disabled, ungranted,
                    unreachable, or unparseable).

Every path that actually calls the model writes exactly one
``chat.orchestrator_brief`` telemetry row. The disabled/ungranted path writes no
row and reads no key: per spec section 1, an ungranted channel produces no
OpenRouter traffic at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from sqlmodel import Session

from app.db import get_engine
from chat import acl, orchestrator_client
from chat.models import OrchestratorBrief

logger = logging.getLogger(__name__)

_BUNDLE_PATH = Path(__file__).with_name("orchestrator_bundle.md")

# Feature name for the ADR 029 consent grant that gates the orchestrator tier.
ORCHESTRATOR_FEATURE = "orchestrator"

# The goose brief keys that must be present for a goose verdict to be usable.
# Missing any of them forces a fail-open (the guest still routes itself from the
# raw prompt), so the model is instructed to fill every field.
_REQUIRED_GOOSE_KEYS = (
    "recipe",
    "repo",
    "repo_paths",
    "hints",
    "constraints",
    "done_criteria",
    "stages",
)


class BriefParseError(Exception):
    """Raised when the model's output cannot be parsed into a valid verdict for
    the active route. The caller (:func:`compile`) turns this into a fail-open."""


@dataclass(frozen=True)
class Directive:
    """The active channel directive, referenced by version for cache stability."""

    version: int = 0
    text: str = ""


@dataclass
class Brief:
    """A compiled goose task brief (spec section 3). ``stages`` is the ordered
    list of stage titles used to pre-render the ADR 035 checklist. ``repo`` is
    advisory: when the model named a repo outside the invoker's ADR 029 scopes
    it is discarded and ``repo_replaced`` is set for logging."""

    recipe: str
    repo: str
    repo_paths: list[str]
    hints: str
    constraints: str
    done_criteria: list[str]
    stages: list[str]
    repo_replaced: bool = False


@dataclass
class ChatVerdict:
    """A conversational verdict carrying the model's reply guidance (ADR 036
    chat-route amendment). The local-Qwen concierge authors the reply using this
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


@dataclass
class RequestContext:
    """Everything :func:`compile` needs for one escalation.

    ``allowed_scopes`` are the invoker's ADR 029 repo grants; ``invoker_scope``
    is the repo the command was invoked with (used to replace an out-of-scope
    brief repo). ``kg_results`` and ``channel_context`` are volatile grounding
    placed in the ``user`` message only.
    """

    request: str
    guild_id: str = ""
    channel_id: str = ""
    thread_id: str | None = None
    invoker_scope: str = ""
    allowed_scopes: frozenset[str] = frozenset()
    channel_context: str = ""
    kg_results: list[str] = field(default_factory=list)
    directive: Directive = field(default_factory=Directive)


Verdict = Brief | ChatVerdict | FailOpen


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
    kg_results: list[str],
    channel_context: str,
    request: str,
) -> tuple[str, str]:
    """Assemble the (system, user) messages in strict stability order (spec
    section 4).

    ``system`` = baked bundle + the versioned channel directive; it contains no
    timestamps, ids, or unsorted collections, so two consecutive escalations in
    the same channel produce byte-identical ``system`` messages and the
    provider's prefix cache hits. All volatile content (KG results, channel
    context, the request) goes in ``user``. Byte-deterministic: identical inputs
    give identical bytes.
    """
    system = (
        bundle.rstrip("\n")
        + f"\n\n## Channel directive (version {directive.version})\n\n"
        + (directive.text.strip() or "(no channel directive set)")
        + "\n"
    )
    kg_block = "\n".join(f"- {r}" for r in kg_results) if kg_results else "(none)"
    user = (
        "## Knowledge graph results\n\n"
        + kg_block
        + "\n\n## Channel context\n\n"
        + (channel_context.strip() or "(none)")
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


def _extract_stage_titles(stages_raw: object) -> list[str]:
    """Pull ordered stage titles out of the ``stages`` field.

    Accepts the spec shape ``[{"title": "..."}]`` and tolerates a plain list of
    strings. Non-string / blank titles are dropped rather than failing the whole
    parse, since stages are advisory (they only pre-render the checklist).
    """
    if not isinstance(stages_raw, list):
        return []
    titles: list[str] = []
    for stage in stages_raw:
        if isinstance(stage, dict):
            title = stage.get("title")
            if isinstance(title, str) and title.strip():
                titles.append(title.strip())
        elif isinstance(stage, str) and stage.strip():
            titles.append(stage.strip())
    return titles


def _parse_goose(data: dict, allowed_scopes: frozenset[str]) -> Brief:
    missing = [k for k in _REQUIRED_GOOSE_KEYS if k not in data]
    if missing:
        raise BriefParseError(f"goose brief missing required keys: {missing}")

    recipe = data["recipe"]
    if not isinstance(recipe, str) or not recipe.strip():
        raise BriefParseError("goose 'recipe' must be a non-empty string")

    repo = data["repo"] if isinstance(data["repo"], str) else ""
    repo_paths = (
        [str(p) for p in data["repo_paths"]]
        if isinstance(data["repo_paths"], list)
        else []
    )
    hints = str(data.get("hints") or "")
    constraints = str(data.get("constraints") or "")
    done_criteria = (
        [str(d) for d in data["done_criteria"]]
        if isinstance(data["done_criteria"], list)
        else []
    )
    stages = _extract_stage_titles(data["stages"])

    # ADR 029: a brief may only name a repo the invoker already holds. An
    # out-of-scope repo is discarded here (flagged for logging); the caller
    # substitutes the invoker's own scope. A blank repo needs no check.
    repo_replaced = False
    if repo and repo not in allowed_scopes:
        repo = ""
        repo_replaced = True

    return Brief(
        recipe=recipe.strip(),
        repo=repo,
        repo_paths=repo_paths,
        hints=hints,
        constraints=constraints,
        done_criteria=done_criteria,
        stages=stages,
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


def parse_brief(content: str, *, allowed_scopes: frozenset[str]) -> Brief | ChatVerdict:
    """Parse the model's strict-JSON output, route-partitioned (spec section 3).

    ``route: goose`` validates and returns a :class:`Brief`; ``route: chat``
    validates the reply-guidance block and returns a :class:`ChatVerdict`. The
    non-active block may be absent, and unknown keys are tolerated; missing
    required keys for the active route raise :class:`BriefParseError` (which the
    caller turns into a fail-open). A repo outside ``allowed_scopes`` is
    discarded with ``Brief.repo_replaced`` set.
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


def _brief_to_json(brief: Brief) -> dict:
    """Serialize a Brief for the telemetry ``brief_json`` column."""
    return {
        "recipe": brief.recipe,
        "repo": brief.repo,
        "repo_paths": brief.repo_paths,
        "hints": brief.hints,
        "constraints": brief.constraints,
        "done_criteria": brief.done_criteria,
        "stages": brief.stages,
        "repo_replaced": brief.repo_replaced,
    }


def _guidance_to_json(verdict: ChatVerdict) -> dict:
    """Serialize chat reply guidance for the telemetry ``brief_json`` column."""
    return {
        "context": verdict.context,
        "direction": verdict.direction,
        "redirect": verdict.redirect,
    }


def render_brief(brief: Brief) -> str:
    """Render a Brief as the markdown header the guest receives as task input.

    The compiled task input is this markdown plus the raw user prompt (never the
    brief alone: the user's words stay ground truth, spec section 3). Kept
    deterministic and prose-light so it reads as guidance, not gospel.
    """
    lines = [
        "# Task brief (advisory, ADR 036 orchestrator)",
        "",
        f"Recipe: {brief.recipe}",
    ]
    if brief.repo:
        lines.append(f"Repo: {brief.repo}")
    if brief.repo_paths:
        lines.append("")
        lines.append("Relevant paths:")
        lines.extend(f"- {p}" for p in brief.repo_paths)
    if brief.hints:
        lines += ["", "Hints:", brief.hints]
    if brief.constraints:
        lines += ["", "Constraints:", brief.constraints]
    if brief.done_criteria:
        lines += ["", "Done when:"]
        lines.extend(f"- {d}" for d in brief.done_criteria)
    if brief.stages:
        lines += ["", "Planned stages:"]
        lines.extend(f"{i + 1}. {title}" for i, title in enumerate(brief.stages))
    return "\n".join(lines)


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
) -> None:
    """Write exactly one telemetry row. Best-effort: a logging failure must
    never break the escalation flow."""
    try:
        with Session(get_engine()) as session:
            session.add(
                OrchestratorBrief(
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
            )
            session.commit()
    except Exception:
        logger.exception("orchestrator: failed to write telemetry row")


async def compile(ctx: RequestContext) -> Verdict:
    """Run the orchestrator for one escalation and return a verdict.

    Order (spec sections 1-2):

    1. If the tier is disabled OR the channel lacks the ADR 029 consent grant,
       return :class:`FailOpen` WITHOUT calling the client and WITHOUT writing a
       row (no OpenRouter traffic, no key read on that path).
    2. Otherwise assemble the prompt and call the client once. A timeout / HTTP
       error / unparseable output all fail open, each writing one ``failopen``
       telemetry row.
    3. A valid verdict writes one ``goose``/``chat`` row and is returned. An
       out-of-scope brief repo is replaced by the invoker's own scope.
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
        ctx.kg_results,
        ctx.channel_context,
        ctx.request,
    )

    try:
        resp = await orchestrator_client.call(system, user)
    except orchestrator_client.OrchestratorUnavailable as exc:
        reason = f"orchestrator unavailable: {exc}"
        logger.warning("orchestrator: failing open, %s", reason)
        await asyncio.to_thread(
            _record, ctx, "failopen", model, None, 0, None, None, None, reason
        )
        return FailOpen(reason)

    try:
        verdict = parse_brief(resp.content, allowed_scopes=ctx.allowed_scopes)
    except BriefParseError as exc:
        reason = f"unparseable brief: {exc}"
        logger.warning("orchestrator: failing open, %s", reason)
        await asyncio.to_thread(
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
        return FailOpen(reason)

    if isinstance(verdict, Brief):
        if verdict.repo_replaced:
            logger.warning(
                "orchestrator: brief named a repo outside invoker scopes; "
                "using invoker scope %r instead",
                ctx.invoker_scope,
            )
            verdict.repo = ctx.invoker_scope
        await asyncio.to_thread(
            _record,
            ctx,
            "goose",
            model,
            _brief_to_json(verdict),
            resp.latency_ms,
            resp.prompt_tokens,
            resp.completion_tokens,
            resp.cached_tokens,
            None,
        )
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
