from __future__ import annotations

from swarm import config

_queues = None


def get_queues():
    global _queues
    if _queues is None:
        from dbos import Queue
        from agent.routine_jobs import DRAINER_WORKER_COUNT

        _queues = (
            Queue("codex", concurrency=config.codex_concurrency()),
            Queue("merge", concurrency=1),
            Queue("drainer", concurrency=DRAINER_WORKER_COUNT),
        )
    return _queues


def codex_queue():
    return get_queues()[0]


def merge_queue():
    return get_queues()[1]


def drainer_queue():
    return get_queues()[2]


# The merge queue is declared but not used yet: ADR 027's merge gate does not
# exist in code, so this example stops at review.


def _prepare_drainer_workers_once(
    submitter, *, completing_workflow_id=None
) -> list[str] | None:
    """Reserve a bounded deficit, failing closed on any DBOS inventory read error."""
    from agent.routine_jobs import drainer_worker_intents, reserve_drainer_workers

    intents = drainer_worker_intents()
    live = submitter.list_workflows(
        name="drain_cycle",
        queue_name="drainer",
        status=["PENDING", "ENQUEUED"],
        load_input=False,
        load_output=False,
    )
    statuses = {intent["workflow_id"]: None for intent in intents.values()}
    if statuses:
        rows = submitter.list_workflows(
            workflow_ids=list(statuses),
            load_input=False,
            load_output=False,
        )
        for row in rows:
            if row.workflow_id not in statuses:
                raise RuntimeError("DBOS returned an unexpected worker identity")
            statuses[row.workflow_id] = row.status
    return reserve_drainer_workers(
        intents,
        statuses,
        {row.workflow_id for row in live},
        completing_workflow_id=completing_workflow_id,
    )


def prepare_drainer_workers(submitter, *, completing_workflow_id=None) -> list[str]:
    for _ in range(3):
        result = _prepare_drainer_workers_once(
            submitter,
            completing_workflow_id=completing_workflow_id,
        )
        if result is not None:
            return result
    raise RuntimeError("drainer worker inventory changed during three refill attempts")


def enqueue_drainer_workers(submitter, work_ids: list[str], *, queue=None) -> int:
    """Submit exact durable identities. A workflow must call this outside a step."""
    from dbos import SetWorkflowID
    from swarm.drainer import drain_cycle

    if not work_ids:
        return 0
    version = None
    if queue is None:
        version = submitter.get_latest_application_version()["version_name"]
        if not version:
            raise RuntimeError("DBOS has no latest application version")
    for workflow_id in work_ids:
        if queue is not None:
            with SetWorkflowID(workflow_id):
                queue.enqueue(drain_cycle)
        else:
            submitter.enqueue(
                {
                    "workflow_name": "drain_cycle",
                    "queue_name": "drainer",
                    "app_version": version,
                    "workflow_id": workflow_id,
                }
            )
    return len(work_ids)


def refill_drainer_workers(submitter, *, queue=None) -> int:
    return enqueue_drainer_workers(
        submitter, prepare_drainer_workers(submitter), queue=queue
    )
