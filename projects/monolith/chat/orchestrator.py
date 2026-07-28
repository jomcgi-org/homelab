"""ADR 036 orchestrator brief compiler: consent gate, prompt assembly, parse,
and fail-open telemetry.

This is the host-side tier that runs on an escalation candidate before any
Firecracker microVM boots. It assembles a deterministic, cache-friendly prompt
(baked bundle + versioned channel directive as ``system``; the volatile context
as ``user``), calls the OpenRouter client, and turns the reply into one of:

- ``PlanVerdict`` - the request needs a real agent session. The route call
                    yields the repo scope (with ADR 029 out-of-scope
                    replacement); a second ``submit_plan`` tool call then yields
                    a validated typed :class:`Plan` (ordered sub-recipe steps
                    with per-step context). Supersedes ``Brief`` on the goose
                    route (runtime-recipe orchestrator, Task 5).
- ``ChatVerdict`` - the request is conversational; carries the model's reply
                    guidance (context, direction, optional redirect) for the
                    local-Qwen concierge to author the actual reply.
- ``FailOpen``    - degrade to today's direct-submit path (disabled, ungranted,
                    unreachable, unparseable, or an unusable/invalid plan).

Two calls on the goose route, one on the chat route. The first call is the
unchanged route decision (chat vs goose, yielding the goose ``Brief`` used only
for its repo scope); on the goose route a second ``call_tool`` produces the
plan. Every compile that actually calls the model writes exactly one
``chat.orchestrator_brief`` telemetry row (the plan call does NOT write a second
row; the single goose row is augmented with plan fields in ``brief_json``). The
disabled/ungranted path writes no row and reads no key: per spec section 1, an
ungranted channel produces no OpenRouter traffic at all.
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
from chat import acl, orchestrator_client, orchestrator_plan
from chat.models import OrchestratorBrief
from chat.orchestrator_plan import Plan
from goosecracker.api import CATALOG, describe_repos

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
class PlanVerdict:
    """A compiled runtime-plan verdict for the goose route (Task 5).

    Supersedes :class:`Brief` on the goose route. The route-decision call still
    yields the repo scope (with ADR 029 out-of-scope replacement carried here as
    ``repo`` / ``repo_replaced``), but the recipe / hints / stages are replaced
    by ``plan``: a validated typed :class:`Plan` produced by a second
    ``submit_plan`` tool call. The plan (ordered sub-recipe steps with per-step
    context) is what the downstream router renderer specializes; the Brief's own
    recipe/hints/stages are intentionally not carried forward."""

    plan: Plan
    repo: str
    repo_paths: list[str]
    repo_replaced: bool = False
    # Id of the telemetry row this verdict wrote, so start_agent_flow can
    # backfill its thread_id once the session thread exists (see link_thread).
    brief_id: int | None = None


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


Verdict = PlanVerdict | ChatVerdict | FailOpen

# Default replan budget when ``ORCHESTRATOR_REPLAN_TIMEOUT_S`` is unset (Task 8).
_REPLAN_TIMEOUT_DEFAULT_S = 120.0


def _replan_timeout_s() -> float:
    """Timeout budget for a replan call (the capped goose escape hatch, Task 7).

    Reads ``ORCHESTRATOR_REPLAN_TIMEOUT_S`` (default 120s). A replan runs after
    goose has actually opened the task and reported what it learned, so it does
    more grounded construction work than the initial plan and gets a more
    generous budget than the initial-compile timeout (``ORCHESTRATOR_TIMEOUT_S``,
    default 60s, applied by ``orchestrator_client``). A malformed env value falls
    back to the default, matching ``orchestrator_client._read_config``'s
    float-parse guard.
    """
    raw = os.environ.get("ORCHESTRATOR_REPLAN_TIMEOUT_S", "")
    try:
        return float(raw) if raw else _REPLAN_TIMEOUT_DEFAULT_S
    except ValueError:
        return _REPLAN_TIMEOUT_DEFAULT_S


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
        + "When the request needs a repo checkout, set the goose brief's `repo` "
        "to exactly one of these owner/repo ids, and leave it empty only for a "
        "repo-less artifact or build. Never name a repo that is not listed.\n\n"
        + (repo_menu.strip() or "(none)")
        + "\n\n## Request\n\n"
        + request.strip()
        + "\n"
    )
    return system, user


def plan_system_prompt() -> str:
    """Build the ``submit_plan`` (second, goose-route) call's system message.

    This is the plan-construction framing, distinct from the route-decision
    bundle (which stays byte-identical so its provider prefix cache keeps
    hitting). It is validated in spirit against ``scratchpad/probe_submit_plan.py``
    (12/12) and its sub-recipe menu is sourced verbatim from
    ``CATALOG`` so the prose and the tool-schema enum can never
    drift. Byte-deterministic: derived only from the ordered catalog, no
    timestamps/ids, so identical catalogs give identical bytes and this call's
    own prefix stays cache-friendly too.
    """
    lines = [
        "You are the delegation orchestrator in front of a coding agent "
        "(goose/Qwen). The routing tier has already decided this request needs a "
        "real agent session; your only job now is to construct the PLAN for it.",
        "",
        "You never write code yourself, you never invent sub-recipe names, and "
        "you never author recipe YAML. You select an allow-set of sub-recipes and "
        "order them into steps, each step naming exactly one sub-recipe plus the "
        "context that step needs. You may only use these sub-recipes:",
        "",
    ]
    lines.extend(f"- {entry.id} = {entry.description}" for entry in CATALOG.values())
    lines.extend(
        [
            "",
            "Submit the plan by calling the submit_plan tool. Keep plans minimal: "
            "include only the steps the task actually needs, in execution order. "
            "Every step's context must be non-empty and grounded in the request. "
            "Every sub-recipe named by a step must also appear in "
            "enabled_subrecipes.",
        ]
    )
    return "\n".join(lines)


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


def _plan_to_json(verdict: Brief, plan: Plan, plan_latency_ms: int) -> dict:
    """Serialize a goose PlanVerdict for the single telemetry ``brief_json`` row.

    Reuses the existing JSONB column (no new DB column / migration): the repo
    scope carried from the route-decision Brief plus the validated plan and its
    call metrics (step count, plan-call latency). The route call's own token /
    latency accounting stays in the row's dedicated columns.
    """
    return {
        "repo": verdict.repo,
        "repo_paths": verdict.repo_paths,
        "repo_replaced": verdict.repo_replaced,
        "plan": {
            "enabled_subrecipes": list(plan.enabled_subrecipes),
            "steps": [
                {"sub_recipe": s.sub_recipe, "context": s.context} for s in plan.steps
            ],
            "done_criteria": list(plan.done_criteria),
        },
        "plan_step_count": len(plan.steps),
        "plan_latency_ms": plan_latency_ms,
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
    ``goosecracker_sessions`` run it produced. Best-effort and idempotent: a
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
    4. On the goose route, apply the ADR 029 out-of-scope repo replacement, then
       make a SECOND call (``submit_plan`` tool) to construct the plan. A plan
       call that is unavailable, returns nothing usable, or fails
       :func:`orchestrator_plan.validate_plan` fails open (one ``failopen`` row).
       A valid plan writes one ``goose`` row (augmented with plan fields, never a
       second row) and returns a :class:`PlanVerdict`.
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

    if isinstance(verdict, Brief):
        if verdict.repo_replaced:
            logger.warning(
                "orchestrator: brief named a repo outside invoker scopes; "
                "using invoker scope %r instead",
                ctx.invoker_scope,
            )
            verdict.repo = ctx.invoker_scope

        # Second call (goose route only): construct the typed plan. Any failure
        # here fails open to today's baked recipe="agent" path, writing the one
        # telemetry row this compile is allowed (no separate plan-call row).
        try:
            # timeout_s=None => call_tool falls back to the client's configured
            # ORCHESTRATOR_TIMEOUT_S (default 60s). Single source of truth for the
            # whole initial compile: the route call() and this plan call_tool()
            # both draw the same budget from orchestrator_client._read_config.
            plan_args, plan_resp = await orchestrator_client.call_tool(
                plan_system_prompt(),
                user,
                schema=orchestrator_plan.submit_plan_schema(),
                timeout_s=None,
            )
        except orchestrator_client.OrchestratorUnavailable as exc:
            reason = f"plan call unavailable: {exc}"
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

        try:
            # Both parse and validate run inside the guard: a malformed tool
            # payload (e.g. a null context or a dict where a sub-recipe id
            # belongs) must never escape compile. plan_from_dict coerces
            # defensively, and any residual AttributeError/TypeError here still
            # lands on the single-row FailOpen path below rather than unwinding
            # (the "compile always fails open" invariant).
            plan = orchestrator_plan.plan_from_dict(plan_args)
            errors = orchestrator_plan.validate_plan(plan)
        except (AttributeError, TypeError) as exc:
            plan = None
            plan_error = f"plan tool returned an unusable object: {exc}"
        else:
            plan_error = f"invalid plan: {errors}" if errors else ""

        if plan is None or plan_error:
            logger.warning("orchestrator: failing open, %s", plan_error)
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
                plan_error,
            )
            return FailOpen(plan_error, brief_id=brief_id)

        brief_id = await asyncio.to_thread(
            _record,
            ctx,
            "goose",
            model,
            _plan_to_json(verdict, plan, plan_resp.latency_ms),
            resp.latency_ms,
            resp.prompt_tokens,
            resp.completion_tokens,
            resp.cached_tokens,
            None,
        )
        return PlanVerdict(
            plan=plan,
            repo=verdict.repo,
            repo_paths=verdict.repo_paths,
            repo_replaced=verdict.repo_replaced,
            brief_id=brief_id,
        )

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


def _render_prior_plan(plan: Plan) -> str:
    """Compact, deterministic rendering of a plan for the replan user prompt.

    Lists the enabled sub-recipes, the ordered steps (id + its context), and the
    done criteria, so the model can see what it already tried before revising it.
    """
    lines = [
        "Enabled sub-recipes: " + (", ".join(plan.enabled_subrecipes) or "(none)"),
        "",
        "Steps (in order):",
    ]
    if plan.steps:
        lines.extend(
            f"{i + 1}. {step.sub_recipe}: {step.context}"
            for i, step in enumerate(plan.steps)
        )
    else:
        lines.append("(none)")
    if plan.done_criteria:
        lines.append("")
        lines.append("Done criteria:")
        lines.extend(f"- {c}" for c in plan.done_criteria)
    return "\n".join(lines)


async def replan(
    request: str,
    prior_plan: Plan,
    *,
    reason: str,
    what_i_learned: str,
    suggested_focus: str,
    timeout_s: float | None = None,
) -> Plan | None:
    """Produce a revised :class:`Plan` after goose reported the plan misfit.

    The capped host-side replan loop (``goosecracker.runner``) calls this when
    goose emits a populated ``replan`` escape-hatch object. It re-invokes the
    ``submit_plan`` tool call with the ORIGINAL request, a compact rendering of
    the prior plan, and goose's replan feedback (reason / what_i_learned /
    suggested_focus, passed as plain strings so this module never imports
    ``goosecracker.replan``), instructing the model to produce a REVISED minimal
    plan that addresses what was learned.

    Fail-open (Design invariant 2): returns ``None`` on
    :class:`~chat.orchestrator_client.OrchestratorUnavailable`, an unusable tool
    result, or a plan that fails :func:`orchestrator_plan.validate_plan`, so the
    caller finalizes with the current goose result rather than looping. Returns
    the validated :class:`Plan` on success.

    ``timeout_s`` defaults to :func:`_replan_timeout_s` (env
    ``ORCHESTRATOR_REPLAN_TIMEOUT_S``, default 120s); an explicit value overrides
    it (used by tests). This is a separate call OUTSIDE ``compile``: it writes no
    telemetry row and does not touch compile's exactly-one-row-per-compile
    accounting, so replan latency is surfaced only via a log line, never a
    telemetry row (that would break compile's exactly-one-row tests).
    """
    effective_timeout_s = timeout_s if timeout_s is not None else _replan_timeout_s()
    user = (
        "## Original request\n\n"
        + request.strip()
        + "\n\n## The plan you produced (goose could not complete it as decided)\n\n"
        + _render_prior_plan(prior_plan)
        + "\n\n## What goose reported when it hit the escape hatch\n\n"
        + f"Reason it stopped: {reason.strip() or '(none given)'}\n"
        + f"What it learned: {what_i_learned.strip() or '(none given)'}\n"
        + f"Where a revised plan should focus: {suggested_focus.strip() or '(none given)'}\n"
        + "\n## Your task\n\n"
        + "Produce a REVISED minimal plan that addresses what goose learned. Keep "
        + "it minimal: only the steps the task now needs, in execution order, each "
        + "with grounded, non-empty context. Do not repeat a sequence goose already "
        + "reported as unworkable. Submit it with the submit_plan tool."
    )

    try:
        plan_args, plan_resp = await orchestrator_client.call_tool(
            plan_system_prompt(),
            user,
            schema=orchestrator_plan.submit_plan_schema(),
            timeout_s=effective_timeout_s,
        )
    except orchestrator_client.OrchestratorUnavailable as exc:
        logger.warning("orchestrator: replan unavailable, failing open: %s", exc)
        return None

    try:
        plan = orchestrator_plan.plan_from_dict(plan_args)
    except (AttributeError, TypeError) as exc:
        logger.warning("orchestrator: replan returned an unusable object: %s", exc)
        return None

    errors = orchestrator_plan.validate_plan(plan)
    if errors:
        logger.warning("orchestrator: replan invalid, failing open: %s", errors)
        return None
    logger.info(
        "orchestrator: replan produced a valid plan in %dms (budget %.0fs, %d steps)",
        plan_resp.latency_ms,
        effective_timeout_s,
        len(plan.steps),
    )
    return plan
