"""Persistence and archive assembly for product updates."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from auth.api import Principal
from core.db import get_engine
from updates.models import ProductUpdate
from updates.schemas import (
    FacetCount,
    ProductUpdateArchive,
    ProductUpdateSubmission,
    ProductUpdateView,
    Project,
    Technology,
)

GITHUB_REPOSITORY = "jomcgi-org/homelab"
GITHUB_BASE_URL = f"https://github.com/{GITHUB_REPOSITORY}"


class UpdateAlreadyPublished(ValueError):
    """The date or source range is already occupied by another update."""


@contextmanager
def _session(session: Session | None) -> Iterator[Session]:
    if session is not None:
        yield session
        return
    with Session(get_engine()) as owned:
        yield owned


def compare_url(base_sha: str, head_sha: str) -> str:
    """Return the canonical public GitHub comparison for one update range."""
    return f"{GITHUB_BASE_URL}/compare/{base_sha}...{head_sha}"


def _row_from_submission(
    submission: ProductUpdateSubmission, principal: Principal
) -> ProductUpdate:
    payload = submission.model_dump(mode="python")
    return ProductUpdate(
        **payload,
        submitted_by=principal.subject,
        submitted_authority=str(principal.authority),
    )


def _same_submission(row: ProductUpdate, submission: ProductUpdateSubmission) -> bool:
    expected = submission.model_dump(mode="python")
    return all(getattr(row, field) == value for field, value in expected.items())


def publish_update(
    submission: ProductUpdateSubmission,
    principal: Principal,
    *,
    session: Session | None = None,
) -> tuple[ProductUpdate, bool]:
    """Insert an immutable daily entry, returning existing rows idempotently."""
    with _session(session) as db:
        existing_date = db.get(ProductUpdate, submission.published_on)
        if existing_date is not None:
            if _same_submission(existing_date, submission):
                return existing_date, False
            raise UpdateAlreadyPublished(
                f"{submission.published_on.isoformat()} already has a published update"
            )

        existing_head = db.exec(
            select(ProductUpdate).where(
                ProductUpdate.source_head_sha == submission.source_head_sha
            )
        ).first()
        if existing_head is not None:
            raise UpdateAlreadyPublished(
                "source_head_sha already belongs to "
                f"{existing_head.published_on.isoformat()}"
            )

        row = _row_from_submission(submission, principal)
        db.add(row)
        try:
            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raced = db.get(ProductUpdate, submission.published_on)
            if raced is not None and _same_submission(raced, submission):
                return raced, False
            raise UpdateAlreadyPublished(
                "the date or source range was published concurrently"
            ) from exc
        db.refresh(row)
        return row, True


def _view(row: ProductUpdate) -> ProductUpdateView:
    return ProductUpdateView(
        **row.model_dump(),
        source_compare_url=compare_url(row.source_base_sha, row.source_head_sha),
    )


def _facet_counts(rows: list[ProductUpdate], field: str) -> list[FacetCount]:
    counts = Counter(value for row in rows for value in getattr(row, field))
    return [
        FacetCount(value=value, count=count) for value, count in sorted(counts.items())
    ]


def archive(
    *,
    project: Project | None = None,
    technology: Technology | None = None,
    limit: int = 366,
    session: Session | None = None,
) -> ProductUpdateArchive:
    """Return the filtered journal plus unfiltered facet counts."""
    with _session(session) as db:
        rows = list(
            db.exec(
                select(ProductUpdate)
                .order_by(ProductUpdate.published_on.desc())
                .limit(limit)
            ).all()
        )

    filtered = [
        row
        for row in rows
        if (project is None or project.value in row.projects)
        and (technology is None or technology.value in row.technologies)
    ]
    return ProductUpdateArchive(
        updates=[_view(row) for row in filtered],
        projects=_facet_counts(rows, "projects"),
        technologies=_facet_counts(rows, "technologies"),
    )
