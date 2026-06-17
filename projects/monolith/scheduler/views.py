"""Wire shapes for the scheduler API.

The view model intentionally omits the lock columns (``locked_by``, ``locked_at``)
from the underlying ``ScheduledJob`` SQLModel. Those are claim-machinery internals
of ``scheduler.api._claim_next_job``; surfacing them would invite tooling that
races against the SKIP LOCKED claim.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class SchedulerJobView(BaseModel):
    name: str
    interval_secs: int
    ttl_secs: int
    next_run_at: datetime
    last_run_at: datetime | None = None
    last_status: str | None = None
    has_handler: bool
