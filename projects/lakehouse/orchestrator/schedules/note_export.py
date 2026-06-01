"""Schedule: incrementally forward changed notes to the event log.

Drives :class:`ExportNoteChangesWorkflow` on the ``housekeeping`` task queue once
a day to forward note adds/updates/soft-deletes since the watermark (the bootstrap
backfill seeds history; this keeps the lakehouse fresh afterwards).

Two properties make a *scheduled* cadence safe here:

* ``overlap=SKIP`` — if a run is still draining when the next tick fires, skip the
  tick rather than queue a pile-up. Combined with the workflow's stable id this
  guarantees runs never interleave, so the watermark read -> emit -> advance
  sequence is effectively single-threaded (no lost or duplicated deltas).
* the export is idempotent (source-derived ``event_version`` + deduped serving
  fold), so even a skipped/retried run can't corrupt the served view.

Daily is plenty given the corpus churn; bumping to hourly is a one-line cron
change (it's a cheap read-only PG sweep). References the workflow by type-name
string so this module stays independent of the workflow unit.
"""

from __future__ import annotations

import temporalio.client

from projects.lakehouse.orchestrator import TaskQueue
from projects.lakehouse.orchestrator.schedules import ScheduleDefinition

WORKFLOW_TYPE = "ExportNoteChangesWorkflow"
SCHEDULE_ID = "note-export"
# Daily at 04:17 UTC — staggered off the hour to avoid colliding with other
# daily housekeeping jobs.
CRON = "17 4 * * *"

SCHEDULES = [
    ScheduleDefinition(
        schedule_id=SCHEDULE_ID,
        schedule=temporalio.client.Schedule(
            action=temporalio.client.ScheduleActionStartWorkflow(
                WORKFLOW_TYPE,
                id=SCHEDULE_ID,
                task_queue=TaskQueue.HOUSEKEEPING.value,
            ),
            spec=temporalio.client.ScheduleSpec(cron_expressions=[CRON]),
            policy=temporalio.client.SchedulePolicy(
                overlap=temporalio.client.ScheduleOverlapPolicy.SKIP,
            ),
        ),
    ),
]
