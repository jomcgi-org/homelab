"""Shared per-hour TTL cutoff for forecast-style hour entries.

The stars domain (and a future hikes change) stores hour-keyed forecast rows
and treats each entry as valid only for the duration of its clock hour. An
entry is dropped once the next hour begins. The cutoff is the top of the
current UTC clock hour: keep entries whose hour_time >= top_of_hour(), drop
the rest. Centralised here so the refresh job, the prune job, and the read
endpoint all agree on the same boundary.
"""

from datetime import datetime, timezone


def top_of_hour(now: datetime | None = None) -> datetime:
    """Return the start of the current UTC clock hour.

    Truncates minute, second, and microsecond to 0 and keeps tzinfo as UTC.
    If ``now`` is None, uses datetime.now(timezone.utc).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    return now.replace(minute=0, second=0, microsecond=0)
