"""Schedule: rotate serving tags onto freshly built artifacts (ADR agents/015).

Drives ``TagRotationWorkflow`` on the ``housekeeping`` task queue every 15
minutes — coupled to the ``build_serving`` cadence so the serving tag advances
to the artifact a build cycle just produced.

References the workflow by **type-name string** so this module is independent of
the WF-DOMAIN unit that defines ``TagRotationWorkflow``.
"""

from __future__ import annotations

import temporalio.client

from projects.lakehouse.orchestrator import TaskQueue
from projects.lakehouse.orchestrator.schedules import ScheduleDefinition

WORKFLOW_TYPE = "TagRotationWorkflow"
SCHEDULE_ID = "tag-rotation"
CRON = "*/15 * * * *"

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
        ),
    ),
]
