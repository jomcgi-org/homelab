"""Shared, caller-owned transaction for ordinary open knowledge disputes."""

from __future__ import annotations

from uuid import uuid4

import yaml
from sqlmodel import Session, select

from knowledge.models import Dispute
from knowledge.raw_write import persist_raw_with_status
from knowledge.store import KnowledgeStore


def create_dispute(
    session: Session,
    fact_id: str,
    reason: str,
    evidence: list[str] | None,
    reporter: dict[str, str],
    *,
    raw_writer=persist_raw_with_status,
    store_factory=KnowledgeStore,
) -> dict:
    """Persist raw and dispute together, without committing or editing the note.

    Callers validate input and own commit/rollback. Injectable persistence keeps
    the existing MCP failure seams and the agents-only import closure intact.
    """
    note = store_factory(session).get_note_by_id(fact_id)
    if note is None:
        return {"error": "unknown fact"}
    existing = session.exec(
        select(Dispute).where(
            Dispute.note_id == fact_id,
            Dispute.reporter_subject == reporter["reporter_subject"],
            Dispute.reason == reason,
            Dispute.state == "open",
        )
    ).first()
    if existing is not None:
        return {
            "dispute_id": existing.id,
            "note_id": fact_id,
            "status": "already-disputed",
            "raw_id": existing.raw_id,
        }
    title = str(note.get("title") or fact_id)
    quoted_title = "\n".join(f"> {line}" for line in title.splitlines())
    body = str(note.get("content") or "")[: 8 * 1024]
    quoted_body = "\n".join(f"> {line}" for line in body.splitlines())
    header = yaml.safe_dump(
        {
            "title": f"Dispute: {title[:80]}",
            "note_id": fact_id,
            "reporter": reporter["reporter_subject"],
            "dispute_nonce": str(uuid4()),
        },
        sort_keys=False,
    ).rstrip()
    pointers = "\n".join(f"- {item}" for item in evidence or [])
    content = (
        f"---\n{header}\n---\n\n## Current fact\n\n"
        f"{quoted_title}\n\n{quoted_body}\n\n## Reason\n\n{reason}\n\n"
        f"## Evidence\n\n{pointers}"
    ).rstrip() + "\n"
    raw, _ = raw_writer(
        session,
        content=content,
        source="dispute",
        original_url=None,
        extra={"note_id": fact_id, **reporter},
        commit=False,
    )
    dispute = Dispute(
        note_id=fact_id,
        raw_id=raw.raw_id,
        reason=reason,
        evidence=evidence or [],
        reporter_subject=reporter["reporter_subject"],
        reporter_authority=reporter["reporter_authority"],
        previous_verification_state=note["verification_state"],
        state="open",
    )
    session.add_all([dispute])
    session.flush()
    return {
        "dispute_id": dispute.id,
        "note_id": fact_id,
        "status": "disputed",
        "raw_id": raw.raw_id,
    }
