"""Wire shapes for the scheduler API.

The view model intentionally omits the legacy lock columns (``locked_by``,
``locked_at``) from the underlying ``ScheduledJob`` SQLModel. Argo owns job
execution, so exposing the old database coordination state would be misleading.
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
