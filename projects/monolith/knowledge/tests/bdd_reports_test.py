"""BDD contracts for the intentional knowledge reporting MCP tools."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel import Session, select

from knowledge.models import Chunk, Dispute, Note, RawInput
from knowledge.store import KnowledgeStore
from shared.testing.markers import covers_public


@covers_public("knowledge.mcp.report_knowledge")
@pytest.mark.asyncio
async def test_report_knowledge_queues_unverified_evidence(
    session, knowledge_mcp_engine
):
    from knowledge.mcp import report_knowledge

    uploads = {}
    with patch(
        "knowledge.ingest_queue.upload_raw",
        side_effect=lambda raw_id, content: uploads.setdefault(raw_id, content),
    ):
        result = await report_knowledge("A repo claim", evidence=["a source"])

    raw = session.exec(
        select(RawInput).where(RawInput.raw_id == result["raw_id"])
    ).one()
    assert result["created"] is True
    assert result["status"] == "queued"
    assert result["scope"] == "repo:jomcgi-org/homelab"
    assert raw.source == "agent-report"
    assert raw.raw_id in uploads


@covers_public("knowledge.mcp.dispute_fact")
@pytest.mark.asyncio
async def test_dispute_fact_records_open_dispute(session, knowledge_mcp_engine):
    from knowledge.mcp import dispute_fact

    vector = [0.1] * 1024
    with Session(knowledge_mcp_engine) as setup:
        note = Note(
            note_id="bdd-disputed-fact",
            path="bdd-disputed-fact.md",
            title="BDD disputed fact",
            content_hash="bdd-disputed-fact-hash",
            content="Current fact body",
            type="fact",
            verification_state="verified",
        )
        setup.add(note)
        setup.flush()
        setup.add(
            Chunk(
                note_fk=note.id,
                chunk_index=0,
                section_header="",
                chunk_text="Current fact body " * 10,
                embedding=vector,
            )
        )
        setup.commit()

    uploads = {}
    with patch(
        "knowledge.ingest_queue.upload_raw",
        side_effect=lambda raw_id, content: uploads.setdefault(raw_id, content),
    ):
        result = await dispute_fact(
            "bdd-disputed-fact", "Contradicted by current evidence"
        )

    raw = session.exec(
        select(RawInput).where(RawInput.raw_id == result["raw_id"])
    ).one()
    dispute = session.exec(
        select(Dispute).where(Dispute.id == result["dispute_id"])
    ).one()
    search_result = next(
        item
        for item in KnowledgeStore(session).search_notes_with_context(vector)
        if item["note_id"] == "bdd-disputed-fact"
    )
    assert raw.source == "dispute"
    assert dispute.state == "open"
    assert dispute.raw_id == raw.raw_id
    assert search_result["disputed"] is True
    assert raw.raw_id in uploads


@covers_public("knowledge.mcp.report_distress")
@pytest.mark.asyncio
async def test_report_distress_records_then_notifies(session, knowledge_mcp_engine):
    from knowledge.mcp import report_distress

    notify = AsyncMock(return_value={"ok": True})
    uploads = {}
    with (
        patch(
            "knowledge.ingest_queue.upload_raw",
            side_effect=lambda raw_id, content: uploads.setdefault(raw_id, content),
        ),
        patch("knowledge.mcp.notify", notify),
    ):
        result = await report_distress(
            "Blocked on access", "urgent", requested_intervention="restore it"
        )

    raw = session.exec(
        select(RawInput).where(RawInput.raw_id == result["intervention_id"])
    ).one()
    assert result["status"] == "notified"
    assert raw.source == "distress"
    assert raw.raw_id in uploads
    notify.assert_awaited_once_with(
        f"raw {raw.raw_id} | distress(urgent) from anonymous: Blocked on access"
        " | wants: restore it",
        "error",
    )
