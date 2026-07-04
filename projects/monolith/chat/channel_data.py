"""Structured dataset extraction from a chat channel window -- pure,
caller-injected.

Pure: takes messages and an injectable async LLM caller, returns a JSON
string (or None on any failure). No Discord or DB imports, so it needs no
session fixture to test. Callers own fetching the window
(chat.store.MessageStore.fetch_window) and building the caller
(chat.summarizer.build_llm_caller); this module only formats, prompts, and
validates the model's structured reply.

The returned source_window metadata is computed from the input messages,
never trusted from the model reply, so a downstream consumer (the
Firecracker guest reading /injected-context/channel-data.json) can rely on
it.
"""

import json
import logging
from collections.abc import Awaitable, Callable

from chat.models import Message

logger = logging.getLogger(__name__)

# Row cap on the extracted dataset. A reply over this is rejected rather than
# truncated: truncating silently would let a caller present a partial dataset
# as complete.
MAX_ROWS = 200

_EXTRACT_PROMPT = (
    "The user wants a structured dataset extracted from the following "
    "Discord conversation window. Reply with STRICT JSON ONLY (no markdown, "
    "no preamble, no code fences) in this exact shape:\n"
    '{{"title": "short dataset title", "columns": ["col1", "col2", ...], '
    '"rows": [["value", "value", ...], ...]}}\n'
    "Every row must have exactly one value per column, in the same order as "
    "columns. Keep it to at most {max_rows} rows; if there are more "
    "candidates, pick the {max_rows} most relevant.\n\n"
    'User request: "{request}"\n\n'
    "Messages:\n{messages}"
)


def _format_lines(messages: list[Message]) -> list[str]:
    return [
        f"[{m.created_at.strftime('%H:%M')}] {m.username}: {m.content}"
        for m in messages
    ]


def _extract_json(raw: str) -> str:
    """Pull the first {...} object out of a model reply (tolerates code
    fences and stray text around the JSON)."""
    s = raw.find("{")
    e = raw.rfind("}")
    return raw[s : e + 1] if s != -1 and e != -1 and e > s else raw


def _validated_fields(data: object) -> tuple[str, list[str], list[list[object]]] | None:
    """Check the parsed reply against the extract_dataset contract, returning
    the (title, columns, rows) tuple on success or None on any violation."""
    if not isinstance(data, dict):
        return None

    title = data.get("title")
    if not isinstance(title, str) or not title:
        return None

    columns = data.get("columns")
    if (
        not isinstance(columns, list)
        or not columns
        or not all(isinstance(c, str) for c in columns)
    ):
        return None

    rows = data.get("rows")
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        return None
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            return None

    return title, columns, rows


async def extract_dataset(
    messages: list[Message],
    request: str,
    caller: Callable[[str], Awaitable[str]],
) -> str | None:
    """Extract a structured title/columns/rows dataset from a message window.

    ``messages`` must be chronological oldest-first (fetch_window's
    contract). On success returns a JSON string of {"title", "columns",
    "rows", "source_window"}, where source_window ({"messages": N, "oldest":
    ISO timestamp of the oldest message}) is computed from the input
    messages, never trusted from the model reply. Returns None if messages is
    empty (without calling caller), if the reply fails to parse or validate,
    or if caller raises: this is a fail-open contract, so a caller can treat
    None as "no dataset, dispatch proceeds without one".
    """
    if not messages:
        return None

    lines = _format_lines(messages)
    prompt = _EXTRACT_PROMPT.format(
        max_rows=MAX_ROWS, request=request, messages="\n".join(lines)
    )

    try:
        raw = await caller(prompt)
        data = json.loads(_extract_json(raw))
    except Exception:
        logger.exception("channel_data: extract_dataset caller/parse failed")
        return None

    validated = _validated_fields(data)
    if validated is None:
        logger.warning("channel_data: extract_dataset reply failed validation")
        return None
    title, columns, rows = validated

    result = {
        "title": title,
        "columns": columns,
        "rows": rows,
        "source_window": {
            "messages": len(messages),
            "oldest": messages[0].created_at.isoformat(),
        },
    }
    return json.dumps(result)
