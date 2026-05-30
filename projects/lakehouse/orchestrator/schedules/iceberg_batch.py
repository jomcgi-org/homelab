"""Schedule: commit buffered events to Iceberg (ADR agents/015).

Drives ``IcebergBatchCommitWorkflow`` on the ``iceberg-builder`` task queue at a
tight cadence so the lakehouse stays near-real-time. An interval (every 90s) is
used rather than cron: sub-minute granularity isn't expressible in 5-field cron,
and an interval matches the "every 1-2 min" batch-commit intent directly.

References the workflow by **type-name string** so this module is independent of
the WF-STORAGE unit that defines ``IcebergBatchCommitWorkflow``.
"""

from __future__ import annotations

from datetime import timedelta

import temporalio.client

from projects.lakehouse.orchestrator import TaskQueue
from projects.lakehouse.orchestrator.schedules import ScheduleDefinition

WORKFLOW_TYPE = "IcebergBatchCommitWorkflow"
SCHEDULE_ID = "iceberg-batch-commit"
INTERVAL = timedelta(seconds=90)

SCHEDULES = [
    ScheduleDefinition(
        schedule_id=SCHEDULE_ID,
        schedule=temporalio.client.Schedule(
            action=temporalio.client.ScheduleActionStartWorkflow(
                WORKFLOW_TYPE,
                id=SCHEDULE_ID,
                task_queue=TaskQueue.ICEBERG_BUILDER.value,
            ),
            spec=temporalio.client.ScheduleSpec(
                intervals=[temporalio.client.ScheduleIntervalSpec(every=INTERVAL)],
            ),
        ),
    ),
]
