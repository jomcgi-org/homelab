"""Light intent classification for the WhatsApp household capabilities (ADR 039
spec section 5). Keyword-first and deterministic: the acceptance phrasings
("record: ...", "add dinner ... Friday 7pm", "remind us to ...") all match a
keyword, so v1 routes on keywords with no per-message LLM call. An injectable
``_caller`` seam is kept for a future natural-phrasing pass, but production stays
keyword-only (cheap, deterministic, no extra classify latency); anything that
misses the keywords falls through to the normal depth/chat path, which can still
help conversationally.

Returned intent is one of: ``record``, ``schedule``, ``reminder``, ``none``.
"""

from __future__ import annotations

import re

# Record: an explicit "record:" prefix, or a natural "note/log/remember that ..."
# phrasing. Kept narrow so ambient chatter is never treated as a capture request
# (the capture itself is still confirmed before anything is written).
_RECORD_PREFIXES = ("record:", "record that", "record ", "log:", "log that ")
_RECORD_PHRASES = (
    "note that",
    "make a note",
    "log that",
    "for the record",
    "remember that",
    "add to the knowledge",
    "save this",
    "note down",
)

# Reminder: checked before schedule, since "remind me to book the table Friday"
# names both a reminder verb and an event verb but is a reminder.
_REMINDER_PHRASES = ("remind ", "reminder", "don't forget", "dont forget")

# Schedule: an explicit calendar word, or an event verb paired with a time-ish
# token (a weekday, today/tomorrow/tonight, or a clock time). The verb-plus-time
# rule catches "add dinner with Sam Friday 7pm" without a bare "add" hijacking
# every message.
_CALENDAR_PHRASES = (
    "calendar",
    "schedule ",
    "appointment",
    "on the cal",
    "to my cal",
    "book a table",
    "book the table",
)
_EVENT_VERBS = (
    "add ",
    "book ",
    "put ",
    "set up ",
    "plan ",
    "dinner",
    "lunch",
    "brunch",
    "drinks",
    "meeting",
    "meet ",
    "party",
)
_TIMEY = re.compile(
    r"\b(mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun"
    r"|monday|tuesday|wednesday|thursday|friday|saturday|sunday"
    r"|today|tomorrow|tonight|noon|midnight|morning|afternoon|evening)\b"
    r"|\b\d{1,2}(:\d{2})?\s*(am|pm)\b|\bat\s+\d{1,2}\b",
    re.IGNORECASE,
)


def _looks_timey(text: str) -> bool:
    return _TIMEY.search(text) is not None


def classify_intent(text: str) -> str:
    """Classify a household message into a capability intent (keyword-only)."""
    t = (text or "").strip().lower()
    if not t:
        return "none"

    if t.startswith(_RECORD_PREFIXES) or any(p in t for p in _RECORD_PHRASES):
        return "record"

    if any(p in t for p in _REMINDER_PHRASES):
        return "reminder"

    if any(p in t for p in _CALENDAR_PHRASES):
        return "schedule"
    if any(v in t for v in _EVENT_VERBS) and _looks_timey(t):
        return "schedule"

    return "none"
