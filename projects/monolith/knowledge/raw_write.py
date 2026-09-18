"""Agents-safe persistence helpers for knowledge raw inputs."""

from __future__ import annotations

from collections import Counter
import hashlib
import logging

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from knowledge.extraction import (
    EXTRACTABLE_SOURCES,
    LANE_OWNED_SOURCES,
    enqueue_extraction,
)
from knowledge.models import RawInput
from knowledge.raw_store import upload_raw
from knowledge.redact import _redact_extra_strings, redact_text_counts

logger = logging.getLogger("monolith.knowledge.raw_write")


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
    status: str | None = None,
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


def persist_raw_with_status(
    session: Session,
    *,
    content: str,
    source: str,
    scope: str | None = None,
    evidence: list[str] | str | None = None,
    validity_hint: str | None = None,
    status: str | None = None,
    original_url: str | None = None,
    extra: dict | None = None,
    commit: bool = True,
    row_writer=write_raw,
) -> tuple[RawInput, bool]:
    """Redact and persist one raw, upload its body, and queue extraction."""
    stored_extra = dict(extra or {})
    if source in LANE_OWNED_SOURCES:
        content, content_redactions = redact_text_counts(content)
        server_redactions: Counter[str] = Counter(content_redactions)
        stored_extra = _redact_extra_strings(stored_extra, server_redactions)
        scope = _redact_extra_strings(scope, server_redactions)
        evidence = _redact_extra_strings(evidence, server_redactions)
        validity_hint = _redact_extra_strings(validity_hint, server_redactions)
        stored_extra["server_redactions"] = dict(server_redactions)

    raw, created = row_writer(
        session,
        content=content,
        source=source,
        scope=scope,
        evidence=evidence,
        validity_hint=validity_hint,
        status=status,
        original_url=original_url,
        extra=stored_extra,
        commit=False,
    )
    if not created:
        return raw, False

    upload_raw(raw.content_hash, content)
    if source in EXTRACTABLE_SOURCES:
        enqueue_savepoint = session.begin_nested()
        try:
            enqueue_extraction(session, raw.raw_id, commit=False)
        except Exception:  # noqa: BLE001 - sweep_unqueued_raws repairs missed jobs
            enqueue_savepoint.rollback()
            logger.exception("raw_write: failed to enqueue raw %s", raw.raw_id)
        else:
            enqueue_savepoint.commit()
    if commit:
        session.commit()
    logger.info("raw_write: persisted raw %s (source=%s)", raw.raw_id, source)
    return raw, True
