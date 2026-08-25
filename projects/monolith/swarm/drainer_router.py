from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from agent.config import drainer_enabled
from swarm import runtime
from swarm.drainer import drain_cycle
from swarm.queues import drainer_queue

router = APIRouter(prefix="/internal/agent", tags=["agent-internal"])


@router.post("/drain", status_code=202)
def trigger_drain(response: Response) -> dict:
    if not drainer_enabled():
        response.status_code = 200
        return {"status": "disabled"}
    if not runtime.is_launched():
        raise HTTPException(
            status_code=503, detail="DBOS is not launched on this replica"
        )
    if runtime.init_dbos() is None:
        raise HTTPException(status_code=503, detail="DBOS is not configured")
    drainer_queue().enqueue(drain_cycle)
    return {"status": "started"}
