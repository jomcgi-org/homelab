"""Play embedding projections, same-transaction writes, and bounded job behavior."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from grimoire import jobs
from grimoire.models import Embedding, Note, SessionEvent
from grimoire.play_embeddings import (
    audience_columns,
    collect_play_inputs,
    content_hash,
    event_embedding_kind,
    event_text,
    note_text,
    persist_play_vectors,
    sync_event_embeddings,
    sync_note_embeddings,
)
from grimoire.testing.leak_harness import sqlite_harness


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
    with sqlite_harness(tmp_path / "play.db") as h:
        # These tests control embedding insertion and reveal-event creation.
        # Retrieval tests separately exercise the harness's seeded stale rows.
        for row in h.session.exec(
            select(Embedding).where(Embedding.campaign_id.is_not(None))
        ).all():
            h.session.delete(row)
        for key in ("reveal_b", "reveal_partial", "reveal_name_only"):
            h.session.delete(h.rows[key])
        h.session.commit()
        yield h


def embedding(session, row, model="test"):
    text = note_text(row) if isinstance(row, Note) else event_text(row)
    result = Embedding(
        embeddable_kind="note" if isinstance(row, Note) else event_embedding_kind(row),
        embeddable_id=row.id,
        model=model,
        dim=1024,
        vector=[0.1] * 1024,
        content_hash=content_hash(text),
        **audience_columns(row),
    )
    session.add(result)
    session.commit()
    return result.id


def stored(h, embedding_id):
    with Session(h.session.get_bind()) as fresh:
        row = fresh.get(Embedding, embedding_id)
        return (
            None
            if row is None
            else {
                **audience_columns_for_embedding(row),
                "content_hash": row.content_hash,
            }
        )


def audience_columns_for_embedding(row):
    return {
        key: getattr(row, key)
        for key in (
            "campaign_id",
            "audience",
            "audience_pc_ids",
            "author_member_id",
            "dm_readable",
        )
    }


def prefix(h):
    return f"/api/grimoire/campaigns/{h.rows['campaign'].id}"


def test_patch_note_copies_audience_and_delete_removes_embedding_in_same_commit(
    harness,
):
    h = harness
    note = h.rows["note_private"]
    eid = embedding(h.session, note)
    with TestClient(h.app()) as client:
        response = client.patch(
            prefix(h) + f"/notes/{note.id}",
            headers=h.headers("player_a"),
            json={"dm_readable": True},
        )
        assert response.status_code == 200, response.text
        assert stored(h, eid)["dm_readable"] is True
        response = client.delete(
            prefix(h) + f"/notes/{note.id}", headers=h.headers("player_a")
        )
        assert response.status_code == 204
        assert stored(h, eid) is None


def test_note_kind_change_helper_copies_every_model_and_rolls_back(harness):
    h = harness
    note = h.rows["note_private"]
    eids = [embedding(h.session, note, model) for model in ("old", "new")]
    note.kind = "party"
    sync_note_embeddings(h.session, note)
    h.session.commit()
    assert all(stored(h, eid)["audience"] == "party" for eid in eids)
    note.kind = "character"
    note.dm_readable = True
    sync_note_embeddings(h.session, note)
    h.session.flush()
    h.session.rollback()
    assert all(stored(h, eid)["audience"] == "party" for eid in eids)
    assert all(stored(h, eid)["dm_readable"] is False for eid in eids)


def test_failed_note_route_commit_rolls_back_embedding(harness, monkeypatch):
    h = harness
    note = h.rows["note_private"]
    note_id = note.id
    eid = embedding(h.session, note)
    before = stored(h, eid)

    def fail_commit():
        h.session.flush()
        h.session.rollback()
        raise RuntimeError("simulated pre-commit failure")

    monkeypatch.setattr(h.session, "commit", fail_commit)
    with TestClient(h.app(), raise_server_exceptions=False) as client:
        response = client.patch(
            prefix(h) + f"/notes/{note_id}",
            headers=h.headers("player_a"),
            json={"dm_readable": True},
        )
    assert response.status_code == 500
    assert stored(h, eid) == before


def test_grouped_reveal_revocation_invalidates_embedding_in_route_transaction(harness):
    h = harness
    h.prepare("play")
    with TestClient(h.app()) as client:
        response = client.post(
            prefix(h) + "/grants/bulk",
            headers=h.headers("dm"),
            json={
                "grants": [
                    {
                        "entity_id": h.rows[key].id,
                        "player_character_id": h.rows["character_a"].id,
                        "grant_scope": "partial",
                        "revealed_details": {"description": key},
                    }
                    for key in ("ancestry", "class")
                ]
            },
        )
        assert response.status_code == 200, response.text
        grants = response.json()
        event = h.session.exec(
            select(SessionEvent).where(SessionEvent.kind == "reveal")
        ).one()
        eid = embedding(h.session, event)
        response = client.delete(
            prefix(h) + f"/grants/{grants[0]['id']}", headers=h.headers("dm")
        )
        assert response.status_code == 204, response.text
        assert stored(h, eid) is None
        assert event.retracted_at is None
        assert "ancestry" not in event_text(event)
        eid = embedding(h.session, event)
        response = client.delete(
            prefix(h) + f"/grants/{grants[1]['id']}", headers=h.headers("dm")
        )
        assert response.status_code == 204
        assert stored(h, eid) is None
        assert event.retracted_at is not None


def event(h, kind, body, **changes):
    row = SessionEvent(
        campaign_id=h.rows["campaign"].id,
        session_id=h.rows["campaign_session"].id,
        seq=100 + len(h.session.exec(select(SessionEvent)).all()),
        kind=kind,
        audience="table",
        audience_pc_ids=[],
        body=body,
        **changes,
    )
    h.session.add(row)
    h.session.commit()
    return row


class FakeEmbedder:
    model = "play-test"

    def __init__(self):
        self.inputs = []
        self.batches = []

    async def embed_batch(self, texts):
        self.inputs.extend(texts)
        self.batches.append(texts)
        return [[float(len(text)) / 1000] * 1024 for text in texts]


def run_job(h, monkeypatch, client, limit=500):
    monkeypatch.setattr(jobs, "_embedding_client", lambda: client)
    monkeypatch.setattr("core.db.get_engine", lambda: h.session.get_bind())
    monkeypatch.setenv("GRIMOIRE_EMBED_PLAY_LIMIT", str(limit))
    asyncio.run(jobs.grimoire_embed_play(None))


def test_job_embeds_only_eligible_player_safe_projections_and_second_run_is_noop(
    harness, monkeypatch
):
    h = harness
    client = FakeEmbedder()
    rows = [
        event(
            h,
            kind,
            {"title": "T", "markdown": "searchable-handout"}
            if kind == "handout"
            else {"text": f"searchable-{kind}"},
        )
        for kind in ("narration", "handout", "utterance")
    ]
    partial = event(
        h,
        "reveal",
        {
            "reveals": [
                {
                    "entity_id": str(uuid4()),
                    "grant_scope": "partial",
                    "name": "Known",
                    "entity": {
                        "revealed_details": {"description": "VISIBLE-DETAIL"},
                        "dm_secret": "NEVER-EMBED",
                    },
                    "dm_secret": "NEVER-EMBED",
                },
                {
                    "entity_id": str(uuid4()),
                    "grant_scope": "name_only",
                    "name": "RECOGNITION-ONLY",
                },
            ]
        },
    )
    rows.append(partial)
    skipped = [
        event(h, kind, {"text": f"skip-{kind}"})
        for kind in ("action", "roll", "turn", "system")
    ]
    skipped.extend(
        [
            event(h, "utterance", {"text": "skip-ooc", "ooc": True}),
            event(
                h,
                "narration",
                {"text": "skip-retracted"},
                retracted_at=datetime.now(UTC),
            ),
            event(
                h,
                "reveal",
                {
                    "entity_id": str(uuid4()),
                    "grant_scope": "name_only",
                    "name": "RECOGNITION-ONLY",
                },
            ),
        ]
    )
    run_job(h, monkeypatch, client)
    with Session(h.session.get_bind()) as fresh:
        embedded = fresh.exec(
            select(Embedding).where(Embedding.model == client.model)
        ).all()
        by_id = {row.embeddable_id: row for row in embedded}
        assert all(row.id in by_id for row in rows)
        assert by_id[rows[2].id].embeddable_kind == "transcript"
        assert all(row.id not in by_id for row in skipped)
        assert h.rows["note_private"].id in by_id
        assert h.rows["note_deleted_character"].id not in by_id
        assert h.rows["note_deleted_party"].id not in by_id
        for row in rows:
            assert audience_columns_for_embedding(by_id[row.id]) == audience_columns(
                row
            )
    text = "\n".join(client.inputs)
    assert "VISIBLE-DETAIL" in text
    assert "NEVER-EMBED" not in text and "RECOGNITION-ONLY" not in text
    assert all(
        f"searchable-{kind}" in text for kind in ("narration", "handout", "utterance")
    )
    assert "skip-" not in text
    before = list(client.inputs)
    run_job(h, monkeypatch, client)
    assert client.inputs == before


def test_job_reembeds_edits_and_stale_audience_and_cleans_orphans(harness, monkeypatch):
    h = harness
    client = FakeEmbedder()
    run_job(h, monkeypatch, client)
    note = h.rows["note_private"]
    note.markdown = "NEW-NOTE-TEXT"
    note.dm_readable = True
    h.session.commit()
    before = len(client.inputs)
    run_job(h, monkeypatch, client)
    assert len(client.inputs) == before + 1
    assert "NEW-NOTE-TEXT" in client.inputs[-1]
    h.session.expire_all()
    row = h.session.exec(
        select(Embedding).where(
            Embedding.embeddable_id == note.id, Embedding.model == client.model
        )
    ).one()
    assert row.dm_readable is True
    row.dm_readable = False
    orphan = Embedding(
        embeddable_kind="event",
        embeddable_id=str(uuid4()),
        model=client.model,
        dim=1024,
        vector=[0.1] * 1024,
        campaign_id=note.campaign_id,
        audience="table",
        audience_pc_ids=[],
    )
    h.session.add(orphan)
    h.session.commit()
    orphan_id = orphan.id
    run_job(h, monkeypatch, client)
    assert len(client.inputs) == before + 2
    assert stored(h, orphan_id) is None
    note.deleted_at = datetime.now(UTC)
    h.session.commit()
    eid = row.id
    run_job(h, monkeypatch, client)
    assert stored(h, eid) is None


def test_job_run_limit_holds_and_progresses(harness, monkeypatch):
    client = FakeEmbedder()
    run_job(harness, monkeypatch, client, limit=2)
    assert len(client.inputs) == 2
    run_job(harness, monkeypatch, client, limit=2)
    assert len(client.inputs) == 4


@pytest.mark.parametrize("change", ["delete", "edit", "retract", "ooc"])
def test_stale_network_result_cannot_restore_changed_source(harness, change):
    h = harness
    row = event(h, "utterance", {"text": "Before network"})
    inputs = collect_play_inputs(h.session, "race", 500)
    inputs = [item for item in inputs if item.source_id == row.id]
    assert len(inputs) == 1
    if change == "delete":
        h.session.delete(row)
    elif change == "edit":
        row.body = {"text": "After network"}
    elif change == "retract":
        row.retracted_at = datetime.now(UTC)
    else:
        row.body = {"text": "Before network", "ooc": True}
    h.session.commit()
    assert persist_play_vectors(h.session, "race", inputs, [[0.1] * 1024]) == 0


def test_event_audience_change_and_retraction_helper(harness):
    h = harness
    row = event(h, "narration", {"text": "Hello"})
    eid = embedding(h.session, row)
    pc_ids = [h.rows["character"].id, h.rows["character_a"].id]
    row.audience = "pcs"
    row.audience_pc_ids = pc_ids
    sync_event_embeddings(h.session, row)
    h.session.commit()
    assert stored(h, eid)["audience_pc_ids"] == sorted(row.audience_pc_ids)
    with TestClient(h.app()) as client:
        response = client.post(
            prefix(h) + f"/sessions/{row.session_id}/events/{row.id}/retract",
            headers=h.headers("dm"),
        )
    assert response.status_code == 200, response.text
    assert stored(h, eid) is None


def test_partial_empty_projection_never_falls_back_to_full_details(harness):
    row = event(
        harness,
        "reveal",
        {
            "entity_id": str(uuid4()),
            "grant_scope": "partial",
            "name": "Known",
            "entity": {"revealed_details": {}, "secret": "NEVER"},
        },
    )
    assert event_text(row) is None


@pytest.mark.parametrize("change", ["retract", "ooc", "action", "delete", "kind"])
def test_job_removes_existing_event_vectors_when_source_changes(
    harness, monkeypatch, change
):
    h = harness
    client = FakeEmbedder()
    row = event(h, "utterance", {"text": "Once searchable"})
    eid = embedding(h.session, row, client.model)
    if change == "retract":
        row.retracted_at = datetime.now(UTC)
    elif change == "ooc":
        row.body = {"text": "Once searchable", "ooc": True}
    elif change == "action":
        row.kind = "action"
    elif change == "delete":
        h.session.delete(row)
    else:
        row.kind = "narration"
    h.session.commit()
    run_job(h, monkeypatch, client)
    assert stored(h, eid) is None
    if change == "kind":
        with Session(h.session.get_bind()) as fresh:
            result = fresh.exec(
                select(Embedding).where(
                    Embedding.embeddable_id == row.id, Embedding.model == client.model
                )
            ).one()
            assert result.embeddable_kind == "event"


def test_job_batches_inputs_and_caps_long_text_without_reembedding(
    harness, monkeypatch
):
    from grimoire.ingest import (
        EMBED_CHAR_BUDGET,
        EMBED_INPUT_MAX_CHARS,
        EMBED_MAX_BATCH,
    )

    h = harness
    client = FakeEmbedder()
    notes = [
        Note(
            campaign_id=h.rows["campaign"].id,
            kind="party",
            title=f"Long {i}",
            markdown="x" * (EMBED_INPUT_MAX_CHARS + 1),
        )
        for i in range(7)
    ]
    h.session.add_all(notes)
    h.session.commit()
    run_job(h, monkeypatch, client)
    assert len(client.batches) > 1
    assert all(len(batch) <= EMBED_MAX_BATCH for batch in client.batches)
    assert all(sum(map(len, batch)) <= EMBED_CHAR_BUDGET for batch in client.batches)
    assert all(len(value) <= EMBED_INPUT_MAX_CHARS for value in client.inputs)
    before = list(client.inputs)
    run_job(h, monkeypatch, client)
    assert client.inputs == before


def test_player_reveal_text_excludes_retracted_items_and_outer_metadata(harness):
    removed_id = str(uuid4())
    row = event(
        harness,
        "reveal",
        {
            "retracted_entity_ids": [removed_id],
            "dm_secret": "OUTER-SECRET",
            "reveals": [
                {
                    "entity_id": removed_id,
                    "grant_scope": "full",
                    "name": "REMOVED",
                    "entity": {"description": "REMOVED-SECRET"},
                },
                {
                    "entity_id": str(uuid4()),
                    "grant_scope": "full",
                    "name": "Visible",
                    "entity": {"description": "VISIBLE-FULL"},
                },
            ],
        },
    )
    result = event_text(row)
    assert "VISIBLE-FULL" in result
    assert "REMOVED" not in result and "OUTER-SECRET" not in result


def test_network_result_copies_current_audience_without_using_stale_snapshot(harness):
    h = harness
    row = event(h, "narration", {"text": "Unchanged text"})
    inputs = [
        item
        for item in collect_play_inputs(h.session, "race", 500)
        if item.source_id == row.id
    ]
    row.audience = "dm"
    h.session.commit()
    assert persist_play_vectors(h.session, "race", inputs, [[0.1] * 1024]) == 1
    h.session.commit()
    result = h.session.exec(
        select(Embedding).where(
            Embedding.embeddable_id == row.id, Embedding.model == "race"
        )
    ).one()
    assert result.audience == "dm"


def test_wrong_embedding_batch_cardinality_does_not_write(harness):
    inputs = collect_play_inputs(harness.session, "wrong-count", 2)
    with pytest.raises(ValueError, match="wrong number of vectors"):
        persist_play_vectors(harness.session, "wrong-count", inputs, [])
    assert (
        harness.session.exec(
            select(Embedding).where(Embedding.model == "wrong-count")
        ).all()
        == []
    )


def test_handout_text_is_title_and_markdown_only(harness):
    h = harness
    key = "campaigns/c1/handouts/0123456789abcdef.png"
    chunk_id = str(uuid4())
    entity_id = str(uuid4())
    for image in (
        {"source": "upload", "key": key},
        {"source": "chunk", "chunk_id": chunk_id},
    ):
        row = event(
            h,
            "handout",
            {
                "title": "Letter from the baron",
                "markdown": "Meet me at **dusk**.",
                "entity_id": entity_id,
                "image": image,
            },
        )
        text = event_text(row)
        assert text == "Letter from the baron\n\nMeet me at **dusk**."
        for hidden in (key, chunk_id, entity_id, "upload", "source", "image"):
            assert hidden not in text
    # A handout without markdown is still searchable by its title.
    assert event_text(event(h, "handout", {"title": "Map"})) == "Map"


def test_retracting_a_handout_removes_its_embedding(harness):
    h = harness
    row = event(
        h,
        "handout",
        {
            "title": "Letter",
            "markdown": "Secret plan",
            "image": {"source": "chunk", "chunk_id": str(uuid4())},
        },
    )
    eid = embedding(h.session, row)
    assert stored(h, eid) is not None
    with TestClient(h.app()) as client:
        response = client.post(
            prefix(h) + f"/sessions/{row.session_id}/events/{row.id}/retract",
            headers=h.headers("dm"),
        )
    assert response.status_code == 200, response.text
    assert stored(h, eid) is None
