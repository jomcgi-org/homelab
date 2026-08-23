"""Home domain public API facade.

Cross-domain callers import home functionality through this module only, per the
module-boundary rule (projects/monolith/ARCHITECTURE.md, section 2,
enforced by import_boundaries_test). Internals stay in home.schedule and friends.
"""

from __future__ import annotations

__all__ = ["get_today_events"]


def get_today_events(session) -> list[dict]:
    """Today's calendar events from the snapshot row (see home.schedule).

    Imported lazily so callers of this facade do not pull the schedule stack
    (httpx, icalendar) at load time.
    """
    from home.schedule import get_today_events as _get

    return _get(session)
