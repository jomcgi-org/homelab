"""Process-lifetime recall counters exposed by the existing KG health surface."""

from collections import Counter
from threading import Lock

_lock = Lock()
_counts = Counter(
    attempts=0, cache_hits=0, skips=0, timeouts=0, distinct_facts_served=0
)
_served: set[str] = set()


def increment(name: str) -> None:
    with _lock:
        _counts[name] += 1


def record_served(items: list[dict]) -> None:
    with _lock:
        for item in items:
            note_id = item["note_id"]
            if note_id not in _served:
                _served.add(note_id)
                _counts["distinct_facts_served"] += 1


def snapshot() -> dict[str, int]:
    with _lock:
        return dict(_counts)
