"""Schedule: rebuild serving artifacts from the lakehouse (ADR agents/015).

Drives ``BuildServingArtifactWorkflow`` on the ``housekeeping`` task queue every
15 minutes to refresh the query-serving artifacts derived from committed Iceberg
data.

References the workflow by **type-name string** so this module is independent of
the WF-STORAGE / WF-DOMAIN units that define ``BuildServingArtifactWorkflow``.
"""

from __future__ import annotations

import temporalio.client

from projects.lakehouse.orchestrator import TaskQueue
from projects.lakehouse.orchestrator.schedules import ScheduleDefinition

WORKFLOW_TYPE = "BuildServingArtifactWorkflow"
SCHEDULE_ID = "build-serving-artifact"
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
