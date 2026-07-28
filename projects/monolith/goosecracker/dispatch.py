"""Ledger lookup helpers for goosecracker."""

from __future__ import annotations

from goosecracker import threads


def status(thread_id: str) -> dict | None:
    """Return a run\047s serialized ledger row by thread id, or None."""
    row = threads.get_run(thread_id)
    return threads.serialize(row) if row else None
