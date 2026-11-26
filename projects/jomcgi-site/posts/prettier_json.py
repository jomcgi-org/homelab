"""JSON serialization compatible with Prettier's scalar-array layout."""

from __future__ import annotations

import json
from typing import Any


def dumps_prettier_json(
    value: Any,
    *,
    ensure_ascii: bool = True,
    sort_keys: bool = False,
    print_width: int = 80,
) -> str:
    """Serialize ``value`` as indent-2 JSON with Prettier-stable arrays.

    Arrays containing only scalar values stay on one line when the complete
    line, including its indentation and any object key, fits ``print_width``.
    Other arrays and all non-empty objects retain ``json.dumps(..., indent=2)``
    style expansion.
    """

    def encode_scalar(item: Any) -> str:
        return json.dumps(item, ensure_ascii=ensure_ascii)

    def encode(item: Any, level: int, starting_column: int) -> str:
        if isinstance(item, (list, tuple)):
            compact = json.dumps(item, ensure_ascii=ensure_ascii)
            if all(not isinstance(child, (dict, list, tuple)) for child in item):
                if starting_column + len(compact) <= print_width:
                    return compact
            if not item:
                return "[]"
            child_indent = " " * ((level + 1) * 2)
            children = [
                child_indent + encode(child, level + 1, len(child_indent))
                for child in item
            ]
            return "[\n" + ",\n".join(children) + "\n" + " " * (level * 2) + "]"

        if isinstance(item, dict):
            if not item:
                return "{}"
            items = item.items()
            if sort_keys:
                items = sorted(items)
            child_indent = " " * ((level + 1) * 2)
            children = []
            for key, child in items:
                encoded_key = json.dumps(key, ensure_ascii=ensure_ascii)
                prefix = f"{child_indent}{encoded_key}: "
                children.append(prefix + encode(child, level + 1, len(prefix)))
            return "{\n" + ",\n".join(children) + "\n" + " " * (level * 2) + "}"

        return encode_scalar(item)

    return encode(value, 0, 0)
