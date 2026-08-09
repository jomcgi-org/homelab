from __future__ import annotations

from graph import config

_queues = None


def get_queues():
    global _queues
    if _queues is None:
        from dbos import Queue

        _queues = (
            Queue("codex", concurrency=config.codex_concurrency()),
            Queue("merge", concurrency=1),
        )
    return _queues


def codex_queue():
    return get_queues()[0]


def merge_queue():
    return get_queues()[1]


# The merge queue is declared but not used yet: ADR 027's merge gate does not
# exist in code, so this example stops at review.
