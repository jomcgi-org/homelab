"""Schedule: sweep the knowledge-gap drain queue (ADR agents/015).

Periodically kicks the ``GapDrainSweepWorkflow`` to drain pending gaps onto the
``gap-drain`` task queue. Cron every 5 minutes — matches the gap pipeline's
existing scheduler cadence (the auto-research drain it replaces).

References the workflow by **type-name string** so this module is independent of
the WF-DOMAIN unit that defines ``GapDrainSweepWorkflow``.
"""

from __future__ import annotations

import temporalio.client

from projects.lakehouse.orchestrator import TaskQueue
from projects.lakehouse.orchestrator.schedules import ScheduleDefinition

WORKFLOW_TYPE = "GapDrainSweepWorkflow"
SCHEDULE_ID = "gap-drain-sweep"
CRON = "*/5 * * * *"

SCHEDULES = [
    ScheduleDefinition(
        schedule_id=SCHEDULE_ID,
        schedule=temporalio.client.Schedule(
            action=temporalio.client.ScheduleActionStartWorkflow(
                WORKFLOW_TYPE,
                id=SCHEDULE_ID,
                task_queue=TaskQueue.GAP_DRAIN.value,
            ),
            spec=temporalio.client.ScheduleSpec(cron_expressions=[CRON]),
        ),
    ),
]
