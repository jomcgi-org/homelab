"""Shield chat-agent replies from leaked tool-call scaffolding.

The chat route (ADR 036) runs the same tool-enabled concierge agent a direct
mention does, so a small model (Qwen) sometimes emits a ``run_code`` tool call
as plain assistant text that the harness failed to parse. The raw
``<tool_call><arg_key>code</arg_key><arg_value>...</arg_value></tool_call>``
scaffolding (and the Python it wraps) then leaks into the Discord message. It
also likes to embed ``![alt](chart.png)`` markdown image tags that Discord does
not render, even though the file is already attached separately.

Two layers, applied in ``_orchestrator_chat_reply`` before delivery:

1. ``scrub_tool_leak`` always runs: it strips the tool-call tags + wrapped code
   and any markdown image tags, and reports whether tool-call markers were
   present (``leaked``). Markdown-image stripping is cosmetic and does NOT set
   ``leaked`` on its own.
2. ``repair_leaked_reply`` runs the deterministic scrub and, only when a leak
   was detected, hands the raw output back to the model for a bounded number of
   reformat passes (``max_turns``) to recover a clean natural-language answer.
   Every leak occurrence is logged (see ``reply_repair_log``) for later eval and
   prompt iteration.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Substrings that mean the model dumped tool-call scaffolding into its answer.
# Whole set kept in sync with the strip patterns below and mirrored in the
# repair prompt so the model knows exactly what to remove.
_TOOL_LEAK_MARKERS = (
    "<tool_call>",
    "</tool_call>",
    "<arg_key>",
    "<arg_value>",
    "<parameter>",
    "</parameter>",
    "<function=",
)

# A closed <tool_call>...</tool_call> block (the common, well-formed leak).
_TOOL_CALL_BLOCK = re.compile(r"<tool_call>.*?</tool_call>", re.DOTALL)
# An unclosed <tool_call> that runs to the end of the message (a truncated
# leak): everything from the opening tag onward is scaffolding.
_TOOL_CALL_TRUNCATED = re.compile(r"<tool_call>.*\Z", re.DOTALL)
# Arg/parameter blocks that appear without a <tool_call> wrapper; the content
# between them is the raw code, so drop the whole block. A malforming small
# model does not close tags consistently (e.g. <arg_value>...</parameter>), so
# any scaffolding open pairs with any scaffolding close (no backreference);
# non-greedy stops at the first close.
_ARG_BLOCK = re.compile(
    r"<(?:arg_key|arg_value|parameter)>.*?</(?:arg_key|arg_value|parameter)>",
    re.DOTALL,
)
# Any orphaned open/close scaffolding tag left after the block strips.
_ORPHAN_TAG = re.compile(r"</?(?:tool_call|arg_key|arg_value|parameter|function)[^>]*>")
# Markdown image tag: Discord renders it as literal text, and the file is
# attached separately, so it is pure noise.
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
# Collapse the blank-line runs the strips leave behind.
_EXCESS_BLANKS = re.compile(r"\n{3,}")


def scrub_tool_leak(text: str) -> tuple[str, bool]:
    """Strip leaked tool-call scaffolding and markdown image tags from ``text``.

    Returns ``(cleaned, leaked)`` where ``leaked`` is True when tool-call
    markers were present (the signal that gates the model-repair loop and the
    log write). Markdown-image removal always happens but does not set
    ``leaked``: a stray image tag needs no repair, only stripping.
    """
    if not text:
        return "", False
    leaked = any(marker in text for marker in _TOOL_LEAK_MARKERS)
    cleaned = _TOOL_CALL_BLOCK.sub("", text)
    cleaned = _TOOL_CALL_TRUNCATED.sub("", cleaned)
    cleaned = _ARG_BLOCK.sub("", cleaned)
    cleaned = _ORPHAN_TAG.sub("", cleaned)
    cleaned = _MARKDOWN_IMAGE.sub("", cleaned)
    cleaned = _EXCESS_BLANKS.sub("\n\n", cleaned).strip()
    return cleaned, leaked


_REPAIR_PROMPT = (
    "The following assistant message accidentally leaked internal tool-call "
    "scaffolding (XML tags like <tool_call>, <arg_key>, <arg_value>, "
    "<parameter>, or raw code) into what should have been a normal chat reply. "
    "Rewrite it as the clean final answer for the user: natural language only, "
    "no XML tags, no tool-call syntax, and no code block unless the user "
    "explicitly asked for code. If the message describes a chart or image, "
    "refer to it as already attached (do not include a markdown image tag). "
    "Preserve the actual findings and tone; just remove the scaffolding.\n\n"
    "Message to rewrite:\n"
)


@dataclass
class RepairOutcome:
    """Result of scrubbing (and maybe repairing) one chat reply.

    ``final`` is what to deliver. ``leaked`` is whether tool-call scaffolding was
    detected at all (gates logging). ``attempts`` counts model-repair passes
    actually made (0 when the scrub alone sufficed or nothing leaked).
    ``still_dirty`` is True when even the last pass still carried markers, so the
    best scrubbed text is delivered as a floor. ``raw``/``scrubbed`` are kept for
    the log row.
    """

    final: str
    leaked: bool
    attempts: int
    still_dirty: bool
    raw: str
    scrubbed: str

    @property
    def outcome(self) -> str:
        # Only leaked replies are ever logged, so this maps to the DB CHECK
        # values. A detected leak always runs at least one repair pass, so it
        # resolves to clean_after_repair or still_dirty (never scrub-only).
        if not self.leaked:
            return "clean"
        return "still_dirty" if self.still_dirty else "clean_after_repair"


async def repair_leaked_reply(
    raw: str,
    *,
    llm_call: Callable[[str], Awaitable[str]],
    max_turns: int = 2,
) -> RepairOutcome:
    """Always scrub ``raw``; if a tool-call leak was present, run up to
    ``max_turns`` model-repair passes to recover a clean answer.

    Each pass hands the raw output back to ``llm_call`` with a reformat
    instruction and re-scrubs the result, stopping as soon as the scrub finds no
    markers. If every pass still leaks, the best deterministic scrub is returned
    as ``final`` (never worse than the raw). A model-call failure ends the loop
    and falls back to the scrub. Never raises: a repair failure must not block
    the reply.
    """
    scrubbed, leaked = scrub_tool_leak(raw)
    if not leaked:
        return RepairOutcome(
            final=scrubbed,
            leaked=False,
            attempts=0,
            still_dirty=False,
            raw=raw,
            scrubbed=scrubbed,
        )

    best = scrubbed
    still_dirty = True
    attempts = 0
    for attempt in range(1, max(0, max_turns) + 1):
        try:
            rewritten = await llm_call(_REPAIR_PROMPT + raw)
        except Exception:
            logger.exception("reply_sanitize: repair pass %d failed", attempt)
            break
        attempts = attempt
        cand, cand_leaked = scrub_tool_leak(rewritten or "")
        if cand and not cand_leaked:
            best = cand
            still_dirty = False
            break
        # A non-empty candidate that still leaks is no better than the scrub;
        # keep the scrub as the floor and try again (up to the cap).
        if cand and not best:
            best = cand

    return RepairOutcome(
        final=best,
        leaked=True,
        attempts=attempts,
        still_dirty=still_dirty,
        raw=raw,
        scrubbed=scrubbed,
    )
