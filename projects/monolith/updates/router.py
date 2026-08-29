"""Private read API for the product-update archive."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlmodel import Session

from core.db import get_session
from updates import store
from updates.schemas import ProductUpdateArchive, Project, Technology

router = APIRouter(prefix="/api/updates", tags=["updates"])


@router.get("", response_model=ProductUpdateArchive)
def list_updates(
    project: Project | None = None,
    technology: Technology | None = None,
    session: Session = Depends(get_session),
) -> ProductUpdateArchive:
    """Return the private journal, optionally filtered by either facet."""
    return store.archive(
        project=project,
        technology=technology,
        session=session,
    )
