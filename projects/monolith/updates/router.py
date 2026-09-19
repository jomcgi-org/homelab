"""Private read API for the product-update archive."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session

from core.db import get_session
from updates import store
from updates.schemas import ProductUpdateArchive, Project, Technology

router = APIRouter(prefix="/api/updates", tags=["updates"])


@router.get("", response_model=ProductUpdateArchive)
def list_updates(
    project: Project | None = None,
    technology: Technology | None = None,
    month: Annotated[
        str | None,
        Query(
            pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
            description="Edition month in YYYY-MM form. Defaults to newest available.",
        ),
    ] = None,
    full_archive: Annotated[
        bool,
        Query(
            alias="all",
            description="Return all edition bodies for compatibility.",
        ),
    ] = False,
    session: Session = Depends(get_session),
) -> ProductUpdateArchive:
    """Return the private journal, optionally filtered by month and facets."""
    if month is not None:
        try:
            date.fromisoformat(f"{month}-01")
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail="month must be a valid calendar month in YYYY-MM form",
            ) from exc
    if full_archive and month is not None:
        raise HTTPException(
            status_code=422,
            detail="month and all=true cannot be combined",
        )
    return store.archive(
        project=project,
        technology=technology,
        month=month,
        full_archive=full_archive,
        session=session,
    )
