from fastapi import APIRouter, Depends
from sqlmodel import Session

from core.db import get_session
from home.dashboard import build_dashboard

router = APIRouter(prefix="/api/home", tags=["dashboard"])


@router.get("/dashboard")
async def get_dashboard(session: Session = Depends(get_session)) -> dict:
    return await build_dashboard(session)
