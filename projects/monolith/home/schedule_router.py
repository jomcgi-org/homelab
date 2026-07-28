from fastapi import APIRouter, Depends
from sqlmodel import Session

from core.db import get_session
from home.schedule import get_today_events

router = APIRouter(prefix="/api/home/schedule", tags=["schedule"])


@router.get("/today")
def schedule_today(session: Session = Depends(get_session)) -> list[dict]:
    return get_today_events(session)
