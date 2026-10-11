"""Campaign facts validate fresh visibility, replay safely, and retract atomically."""

import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from grimoire import play_embeddings
from grimoire.character_facts import (
    FactCandidate,
    build_fact_payload,
    write_character_facts,
)
from grimoire.models import CharacterFact, Embedding, GameSession, SessionEvent
from grimoire.play_embeddings import (
    collect_play_inputs,
    persist_play_vectors,
    sync_event_embeddings,
    sync_fact_embeddings,
)
from grimoire.testing.leak_harness import sqlite_harness
from grimoire.testing.sql_capture import assert_no_knowledge_sql, capture_sql


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
    with sqlite_harness(tmp_path / "facts.db") as h:
        yield h


def candidate(h, event="event_table", **kwargs):
    return FactCandidate(
        statement="Gundren owes us 10gp",
        evidence_event_ids=[h.rows[event].id],
        **kwargs,
    )


def write(h, candidates, viewer=None, version="test/v1"):
    return write_character_facts(
        h.session,
        h.rows["campaign"].id,
        h.rows["campaign_session"].id,
        viewer or h.rows["character_a"].id,
        version,
        candidates,
    )


def test_accepts_visible_fact_and_replay_preserves_status(harness):
    h = harness
    fact = candidate(
        h, "event_character_a", entity_id=h.rows["a_only"].id, confidence=0.9
    )
    result = write(h, [fact, fact])
    assert not result.rejections and len(result.written) == 1
    stored = result.written[0]
    assert stored.entity_id == h.rows["a_only"].id
    assert stored.evidence_event_ids == [h.rows["event_character_a"].id]
    assert isinstance(stored.created_at, datetime)
    stored.status = "disputed"
    h.session.commit()
    assert write(h, [fact]).written[0].id == stored.id
    assert write(h, [fact]).written[0].status == "disputed"
    assert len(write(h, [fact], version="test/v2").written) == 1


@pytest.mark.parametrize(
    "event",
    (
        "event_dm",
        "event_character",
        "event_foreign",
        "event_table_retracted",
    ),
)
def test_rejects_invisible_foreign_or_retracted_evidence_per_fact(harness, event):
    h = harness
    result = write(h, [candidate(h, event), candidate(h)])
    assert len(result.written) == 1
    assert result.rejections[0].index == 0
    assert "not live and visible" in result.rejections[0].reason


def test_rejects_missing_and_other_session_events(harness):
    h = harness
    game = GameSession(campaign_id=h.rows["campaign"].id, status="ended")
    event = SessionEvent(
        campaign_id=game.campaign_id,
        session_id=game.id,
        seq=1,
        kind="narration",
        audience="table",
        body={"text": "Another session"},
    )
    h.session.add_all([game, event])
    h.session.commit()
    for evidence_id in (str(uuid4()), event.id):
        result = write(
            h, [FactCandidate(statement="invalid", evidence_event_ids=[evidence_id])]
        )
        assert not result.written
        assert "not live and visible" in result.rejections[0].reason


@pytest.mark.parametrize("entity", ("private", "b_only", "foreign"))
def test_rejects_entity_outside_server_vocabulary(harness, entity):
    result = write(harness, [candidate(harness, entity_id=harness.rows[entity].id)])
    assert not result.written
    assert "outside the viewer's vocabulary" in result.rejections[0].reason


@pytest.mark.parametrize(
    "bad",
    (
        {"statement": "empty evidence", "evidence_event_ids": []},
        {"statement": "bad uuid", "evidence_event_ids": ["not-a-uuid"]},
        {"statement": " ", "evidence_event_ids": []},
        {"statement": "bad shape"},
    ),
)
def test_malformed_candidate_is_a_per_fact_rejection(harness, bad):
    result = write(harness, [bad, candidate(harness)])
    assert len(result.written) == 1 and len(result.rejections) == 1


def test_empty_batch_does_not_write(harness):
    h = harness
    with capture_sql(h.session.get_bind()) as sql:
        result = write(h, [])
    assert result.written == [] and result.rejections == []
    assert not any(statement.startswith("INSERT") for statement in sql)


def test_sql_capture_guard_detects_flattened_sqlite_knowledge_access(harness):
    h = harness
    with capture_sql(h.session.get_bind()) as statements:
        h.session.execute(select(SQLModel.metadata.tables["knowledge.notes"])).all()
    assert "knowledge." not in statements[0]
    with pytest.raises(AssertionError, match="notes"):
        assert_no_knowledge_sql(statements)


def test_invalid_viewer_or_session_context_fails_closed(harness):
    h = harness
    for viewer in ("dm", "missing", h.rows["other_character"].id):
        with pytest.raises(ValueError, match="viewer must"):
            write(h, [candidate(h)], viewer=viewer)
    with pytest.raises(ValueError, match="session does not belong"):
        write_character_facts(
            h.session,
            h.rows["other"].id,
            h.rows["campaign_session"].id,
            "party",
            "v1",
            [],
        )


def test_party_rejects_private_evidence_and_private_grounding(harness):
    h = harness
    assert (
        write(h, [candidate(h)], viewer="party").written[0].player_character_id is None
    )
    assert write(h, [candidate(h, "event_character_a")], viewer="party").rejections
    assert write(
        h, [candidate(h, entity_id=h.rows["a_only"].id)], viewer="party"
    ).rejections


def test_payload_is_json_serializable_and_passes_leak_canaries(harness):
    h = harness
    payload = build_fact_payload(
        h.session,
        h.rows["campaign"].id,
        h.rows["campaign_session"].id,
        h.rows["character_a"].id,
    )
    response = httpx.Response(
        200, request=httpx.Request("GET", "https://test/payload"), json=payload
    )
    h.assert_no_leak(response, "player_a")
    serialized = json.dumps(payload)
    assert h.rows["event_character_a"].id in serialized
    assert h.rows["note_private"].markdown in serialized
    assert h.rows["event_table_retracted"].id not in serialized
    party = build_fact_payload(
        h.session,
        h.rows["campaign"].id,
        h.rows["campaign_session"].id,
        "party",
    )
    response = httpx.Response(
        200, request=httpx.Request("GET", "https://test/party"), json=party
    )
    h.assert_no_leak(response, "player_a")
    h.assert_no_leak(response, "player_b")
    assert h.rows["event_character_a"].id not in response.text


def test_payload_transcript_excludes_ooc_other_pc_and_dm(harness):
    h = harness
    rows = []
    for index, (audience, pcs, ooc, token) in enumerate(
        (
            ("table", [], False, "known table speech"),
            ("pcs", [h.rows["character"].id], False, "another PC speech"),
            ("dm", [], False, "DM speech"),
            ("table", [], True, "OOC speech"),
        ),
        100,
    ):
        rows.append(
            SessionEvent(
                campaign_id=h.rows["campaign"].id,
                session_id=h.rows["campaign_session"].id,
                seq=index,
                kind="utterance",
                audience=audience,
                audience_pc_ids=pcs,
                body={"text": token, "ooc": ooc},
            )
        )
    h.session.add_all(rows)
    h.session.commit()
    payload = build_fact_payload(
        h.session,
        h.rows["campaign"].id,
        h.rows["campaign_session"].id,
        h.rows["character_a"].id,
    )
    assert len(payload["utterances"]) == 1
    assert payload["utterances"][0]["body"]["text"] == "known table speech"


def embed_fact(h, fact):
    inputs = [
        item
        for item in collect_play_inputs(h.session, "facts-test", 100)
        if item.source_id == fact.id
    ]
    assert len(inputs) == 1 and inputs[0].kind == "fact"
    assert persist_play_vectors(h.session, "facts-test", inputs, [[0.1] * 1024]) == 1
    h.session.commit()


def test_retraction_cascade_and_embedding_sync_never_touch_knowledge(harness):
    h = harness
    with capture_sql(h.session.get_bind()) as sql:
        only = write(h, [candidate(h)]).written[0]
        both = write(
            h,
            [
                FactCandidate(
                    statement="Two witnesses",
                    evidence_event_ids=[
                        h.rows["event_table"].id,
                        h.rows["event_character_a"].id,
                    ],
                )
            ],
        ).written[0]
        embed_fact(h, only)
        embed_fact(h, both)
        only.status = "disputed"
        sync_fact_embeddings(h.session, only)
        event = h.rows["event_table"]
        event.retracted_at = datetime.now(UTC)
        sync_event_embeddings(h.session, event)
        h.session.commit()
        assert only.status == "retracted"
        assert both.status == "active"
        assert not h.session.exec(
            select(Embedding).where(Embedding.embeddable_id == only.id)
        ).all()
        assert h.session.exec(
            select(Embedding).where(Embedding.embeddable_id == both.id)
        ).all()
        other = h.rows["event_character_a"]
        other.retracted_at = datetime.now(UTC)
        sync_event_embeddings(h.session, other)
        h.session.commit()
        assert both.status == "retracted"
        assert not h.session.exec(
            select(Embedding).where(Embedding.embeddable_id == both.id)
        ).all()
    assert_no_knowledge_sql(sql)


def test_grant_history_retraction_cascades_to_fact(harness):
    from grimoire.router import _retract_grant_history

    h = harness
    fact = write(
        h, [candidate(h, "reveal_partial", entity_id=h.rows["partial"].id)]
    ).written[0]
    embed_fact(h, fact)
    with capture_sql(h.session.get_bind()) as sql:
        _retract_grant_history(h.session, h.rows["grant_partial"])
        h.session.commit()
        assert fact.status == "retracted"
        assert h.rows["reveal_partial"].retracted_at is not None
        assert not h.session.exec(
            select(Embedding).where(Embedding.embeddable_id == fact.id)
        ).all()
    assert_no_knowledge_sql(sql)


def test_partial_grouped_reveal_revocation_invalidates_ambiguous_evidence(harness):
    from grimoire.router import _retract_grant_history

    h = harness
    reveal = h.rows["reveal_partial"]
    other = h.rows["a_only"]
    reveal.body = {
        "reveals": [
            *reveal.body["reveals"],
            {
                "entity_id": other.id,
                "name": other.name,
                "entity_type": "npc",
                "grant_scope": "full",
                "entity": {"description": "Unchanged shared item"},
            },
        ]
    }
    h.session.commit()
    fact = write(
        h, [candidate(h, "reveal_partial", entity_id=h.rows["partial"].id)]
    ).written[0]
    ambiguous = write(
        h,
        [
            FactCandidate(
                statement="No item-level provenance", evidence_event_ids=[reveal.id]
            )
        ],
    ).written[0]
    other_support = write(
        h,
        [
            FactCandidate(
                statement="Other evidence survives",
                evidence_event_ids=[reveal.id, h.rows["event_table"].id],
            )
        ],
    ).written[0]
    embed_fact(h, fact)
    embed_fact(h, ambiguous)
    _retract_grant_history(h.session, h.rows["grant_partial"])
    h.session.commit()
    assert reveal.retracted_at is None  # Surviving grouped item stays in the feed.
    assert fact.status == ambiguous.status == "retracted"
    assert other_support.status == "active"
    assert not h.session.exec(
        select(Embedding).where(Embedding.embeddable_id.in_([fact.id, ambiguous.id]))
    ).all()
    payload = build_fact_payload(
        h.session, fact.campaign_id, fact.session_id, fact.viewer_key
    )
    assert reveal.id not in payload["evidence_event_ids"]
    assert "Unchanged shared item" in json.dumps(payload)
    assert write(h, [candidate(h, "reveal_partial", entity_id=other.id)]).rejections
    h.rows["event_table"].retracted_at = datetime.now(UTC)
    sync_event_embeddings(h.session, h.rows["event_table"])
    h.session.commit()
    assert other_support.status == "retracted"


def test_embedding_lock_order_is_events_before_facts_with_paired_vectors(
    harness, monkeypatch
):
    h = harness
    fact = write(h, [candidate(h)]).written[0]
    inputs = collect_play_inputs(h.session, "ordering", 100)
    event = h.rows["event_table"]
    by_id = {item.source_id: item for item in inputs}
    assert inputs.index(by_id[event.id]) < inputs.index(by_id[fact.id])
    h.session.commit()
    calls = []
    original = play_embeddings._source

    def source(session, kind, source_id):
        calls.append((kind, source_id))
        return original(session, kind, source_id)

    monkeypatch.setattr(play_embeddings, "_source", source)
    # Persistence must impose lock order, even if the caller supplies fact first.
    assert (
        persist_play_vectors(
            h.session,
            "ordering",
            [by_id[fact.id], by_id[event.id]],
            [[0.2] * 1024, [0.1] * 1024],
        )
        == 2
    )
    assert calls == [("event", event.id), ("fact", fact.id)]
    h.session.commit()
    vectors = {
        row.embeddable_id: row.vector
        for row in h.session.exec(
            select(Embedding).where(Embedding.model == "ordering")
        ).all()
    }
    assert vectors[fact.id][0] == pytest.approx(0.2)
    assert vectors[event.id][0] == pytest.approx(0.1)
    calls.clear()
    collect_play_inputs(h.session, "ordering", 100)
    # Cleanup reads and locks existing event/transcript sources before facts too.
    first_fact = next(index for index, (kind, _) in enumerate(calls) if kind == "fact")
    assert not any(kind in ("event", "transcript") for kind, _ in calls[first_fact:])


def test_payload_and_writer_refresh_cached_visibility(harness):
    h = harness
    grant = h.rows["grant_a_only"]
    assert grant.grant_scope == "full"
    with Session(h.session.get_bind()) as fresh:
        current = fresh.get(type(grant), grant.id)
        current.grant_scope = "name_only"
        fresh.commit()
    payload = build_fact_payload(
        h.session,
        h.rows["campaign"].id,
        h.rows["campaign_session"].id,
        h.rows["character_a"].id,
    )
    entity = next(
        item for item in payload["entities"] if item["id"] == h.rows["a_only"].id
    )
    assert entity == {
        "id": h.rows["a_only"].id,
        "name": h.rows["a_only"].name,
        "entity_type": "npc",
        "recognition_only": True,
    }


def test_retraction_rolls_back_fact_and_vector_together(harness):
    h = harness
    fact = write(h, [candidate(h)]).written[0]
    embed_fact(h, fact)
    h.rows["event_table"].retracted_at = datetime.now(UTC)
    sync_event_embeddings(h.session, h.rows["event_table"])
    assert fact.status == "retracted"
    h.session.rollback()
    assert fact.status == "active"
    assert h.session.exec(
        select(Embedding).where(Embedding.embeddable_id == fact.id)
    ).all()


def test_event_retraction_route_cascades_in_same_commit(harness):
    h = harness
    fact = write(h, [candidate(h)]).written[0]
    embed_fact(h, fact)
    fact_id = fact.id
    with TestClient(h.app()) as client:
        response = client.post(
            f"/api/grimoire/campaigns/{fact.campaign_id}/sessions/{fact.session_id}/events/{h.rows['event_table'].id}/retract",
            headers=h.headers("dm"),
        )
    assert response.status_code == 200, response.text
    with Session(h.session.get_bind()) as fresh:
        assert fresh.get(CharacterFact, fact_id).status == "retracted"
        assert not fresh.exec(
            select(Embedding).where(Embedding.embeddable_id == fact_id)
        ).all()


def test_embedding_write_rechecks_retracted_source(harness):
    h = harness
    fact = write(h, [candidate(h)]).written[0]
    inputs = [
        item
        for item in collect_play_inputs(h.session, "facts-test", 100)
        if item.source_id == fact.id
    ]
    fact.status = "retracted"
    assert persist_play_vectors(h.session, "facts-test", inputs, [[0.1] * 1024]) == 0


@pytest.mark.parametrize(
    "changes",
    (
        {"viewer_key": "party"},
        {"player_character_id": None},
        {"status": "unknown"},
        {"evidence_event_ids": []},
        {"evidence_event_ids": [None]},
    ),
)
def test_sqlite_fact_constraints(harness, changes):
    h = harness
    row = CharacterFact(
        campaign_id=h.rows["campaign"].id,
        session_id=h.rows["campaign_session"].id,
        player_character_id=h.rows["character_a"].id,
        viewer_key=h.rows["character_a"].id,
        statement="bad storage",
        evidence_event_ids=[h.rows["event_table"].id],
        extraction_version="v1",
    )
    for key, value in changes.items():
        setattr(row, key, value)
    with pytest.raises(IntegrityError), h.session.begin_nested():
        h.session.add(row)
        h.session.flush()
