from fastapi import APIRouter

from home.dashboard import build_dashboard

router = APIRouter(prefix="/api/home", tags=["dashboard"])


@router.get("/dashboard")
async def get_dashboard() -> dict:
    # No request-scoped session: build_dashboard runs each read in a worker
    # thread with its own session, so the event loop never waits on the pool.
    return await build_dashboard()
