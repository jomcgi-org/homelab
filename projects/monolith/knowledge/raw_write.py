"""Minimal database writer for knowledge raw inputs."""

from __future__ import annotations

import hashlib

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from knowledge.models import RawInput


def _coerce_evidence(evidence: list[str] | str | None) -> list[str] | None:
    """Return evidence in the JSON-list shape stored by raw inputs."""
    if evidence is None:
        return None
    if isinstance(evidence, str):
        return [evidence]
    return [str(item) for item in evidence]


def write_raw(
    session: Session,
    *,
    content: str,
    source: str,
    scope: str | None = None,
    evidence: list[str] | str | None = None,
    validity_hint: str | None = None,
    status: str | None = "queued",
    original_url: str | None = None,
    extra: dict | None = None,
    commit: bool = True,
) -> tuple[RawInput, bool]:
    """Insert one queued raw metadata row and report whether it was created.

    The caller owns content storage and extraction queueing. Keeping those
    concerns outside this module makes the row writer safe for the pruned
    agents tier.
    """
    raw_id = hashlib.sha256(content.encode("utf-8")).hexdigest()
    existing = session.exec(select(RawInput).where(RawInput.raw_id == raw_id)).first()
    if existing is not None:
        return existing, False

    stored_extra = dict(extra or {})
    if status is not None:
        stored_extra.update(
            {
                "scope": scope,
                "evidence": _coerce_evidence(evidence),
                "validity_hint": validity_hint,
                "status": status,
            }
        )
    raw = RawInput(
        raw_id=raw_id,
        path=f"raws/{raw_id}.md",
        source=source,
        content_hash=raw_id,
        original_path=original_url,
        extra=stored_extra,
    )
    savepoint = session.begin_nested()
    try:
        session.add(raw)
        session.flush()
    except IntegrityError:
        savepoint.rollback()
        existing = session.exec(
            select(RawInput).where(RawInput.raw_id == raw_id)
        ).first()
        if existing is None:
            raise
        return existing, False
    else:
        savepoint.commit()
    if commit:
        session.commit()
    return raw, True
