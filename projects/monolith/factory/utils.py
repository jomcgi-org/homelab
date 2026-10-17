"""Shared helpers for factory payloads."""

from __future__ import annotations


def sanitize_payload(obj: object) -> object:
    """Remove NUL bytes from all strings in a payload tree.

    NUL bytes (\\x00) cannot be represented in Postgres text or jsonb. This
    sanitiser recurses through dicts, lists, and tuples, removing NUL from
    every string while leaving the rest of the structure and content intact.

    Non-string, non-container objects pass through unchanged.
    """
    if isinstance(obj, str):
        return obj.replace("\x00", "")
    if isinstance(obj, dict):
        return {
            sanitize_payload(key): sanitize_payload(value) for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [sanitize_payload(item) for item in obj]
    if isinstance(obj, tuple):
        return tuple(sanitize_payload(item) for item in obj)
    return obj
