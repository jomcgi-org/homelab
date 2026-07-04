"""Directive-evolution observer (ADR 035 phase 4): style-friction classifier.

Pure: takes a list of recent user messages directed at the bot and an
injectable async LLM caller, returns a directive-change proposal or None. No
Discord or DB imports, so it needs no session fixture to test. Callers (Task
4.2) own fetching the exchanges (ambient-granted channels only) and wiring
``min_evidence`` from values/env; this module only classifies and prompts.
"""

import json
import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

_FRICTION_PROMPT = (
    "You are reviewing recent messages a user sent to a Discord bot, looking "
    "for RECURRING style friction: the SAME kind of correction repeated more "
    "than once (for example: replies too long, wrong tone, unwanted replies "
    "when the bot should have stayed quiet, formatting complaints). A single "
    "complaint, or several different complaints each raised only once, is "
    "NOT recurring friction: report friction=false in that case.\n\n"
    "If you do find recurring friction, describe the change ONLY in terms of "
    'tone, attention, or interaction style, for example "reply more '
    'concisely" or "stop replying unless directly addressed". NEVER '
    "describe a change to tools, permissions, ACLs, ambient mode, or repo "
    "access: those are out of scope for this classifier and must never "
    "appear in directive_change.\n\n"
    "Cite ONLY message_ids that are genuine evidence of the recurring "
    "complaint, taken from the list below. Never invent an id that is not "
    "in the list.\n\n"
    "Messages (message_id: author: text):\n{exchanges}\n\n"
    'Reply with ONLY a JSON object: {{"friction": true|false, '
    '"directive_change": string, "evidence_message_ids": [string]}}. No '
    "prose, no markdown."
)


def _format_exchanges(exchanges: list[dict]) -> str:
    return "\n".join(
        f"{e['message_id']}: {e['author']}: {e['text']}" for e in exchanges
    )


def _extract_json(raw: str) -> str:
    """Pull the first {...} object out of a model reply (tolerates stray text)."""
    s = raw.find("{")
    e = raw.rfind("}")
    return raw[s : e + 1] if s != -1 and e != -1 and e > s else raw


async def find_style_friction(
    exchanges: list[dict],
    caller: Callable[[str], Awaitable[str]],
    min_evidence: int = 3,
) -> dict | None:
    """Classify recent user exchanges for recurring style friction.

    ``exchanges`` is a chronological-order-agnostic list of dicts
    ``{"message_id": str, "author": str, "text": str}`` representing recent
    user messages directed at the bot (replies/mentions), pre-fetched by the
    caller. Returns ``None`` (fails closed) when: ``exchanges`` is empty (no
    caller call made), the model reports no friction, fewer than
    ``min_evidence`` evidence ids are cited, any cited id is not one of the
    input message_ids (hallucinated evidence), ``directive_change`` is
    empty/missing, the reply is unparseable, or the caller raises (logged).
    On success returns ``{"directive_change": str, "evidence_message_ids":
    [str]}`` with evidence ids deduplicated, order preserved.
    """
    if not exchanges:
        return None
    try:
        known_ids = {e["message_id"] for e in exchanges}
        prompt = _FRICTION_PROMPT.format(exchanges=_format_exchanges(exchanges))
        raw = await caller(prompt)
        data = json.loads(_extract_json(raw))

        if not data.get("friction"):
            return None

        directive_change = data.get("directive_change")
        if not isinstance(directive_change, str) or not directive_change.strip():
            return None

        evidence_ids = data.get("evidence_message_ids")
        if not isinstance(evidence_ids, list) or not evidence_ids:
            return None
        deduped_ids = list(dict.fromkeys(evidence_ids))
        if not set(deduped_ids).issubset(known_ids):
            return None
        if len(deduped_ids) < min_evidence:
            return None

        return {
            "directive_change": directive_change,
            "evidence_message_ids": deduped_ids,
        }
    except Exception:
        logger.exception("observer: style-friction classify failed")
        return None
