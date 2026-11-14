"""Select JSON objects without imposing a domain's fences or result schema."""

from collections.abc import Iterable
import json
from typing import Literal


def _object_spans(text: str) -> list[str]:
    spans = []
    depth = 0
    start = None
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                spans.append(text[start : index + 1])
                start = None
    return spans


def select_json_object(
    text: str, fence_blocks: Iterable[str], *, fallback: Literal["forward", "reverse"]
) -> dict | None:
    """Prefer the last fenced dictionary, then use the caller's fallback order.

    Every fence is decoded forwards. Forward fallback decodes every balanced
    candidate and retains the last dictionary; reverse fallback stops at the
    first dictionary. Only JSON syntax errors are skipped in either traversal.
    None means no selection; an empty dictionary is a successful selection.
    """
    selected = None
    for block in fence_blocks:
        try:
            candidate = json.loads(block)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            selected = candidate
    if selected is not None:
        return selected

    spans = _object_spans(text)
    for span in reversed(spans) if fallback == "reverse" else spans:
        try:
            candidate = json.loads(span)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            selected = candidate
            if fallback == "reverse":
                break
    return selected
