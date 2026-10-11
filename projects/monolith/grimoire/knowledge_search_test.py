"""Knowledge retrieval blocks leaks even when kNN ignores every SQL filter."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql
from sqlmodel import select

from grimoire import search
from grimoire.models import CharacterFact, Embedding, SessionEvent
from grimoire.play_embeddings import audience_columns
from grimoire.testing.leak_harness import (
    ROLES,
    FakeEmbedClient,
    fake_knn,
    sqlite_harness,
)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "true")
    monkeypatch.setattr(search, "knn_embeddings", fake_knn)
    with sqlite_harness(tmp_path / "knowledge.db") as h:
        yield h


def request(client, h, viewer, **params):
    response = client.get(
        f"/api/grimoire/campaigns/{h.rows['campaign'].id}/knowledge/search",
        headers=h.headers(viewer),
        params={"q": "knowledge", "k": 50, **params},
    )
    h.assert_no_leak(response, viewer)
    return response


@pytest.mark.parametrize("viewer", ROLES)
def test_private_note_positive_control_and_no_other_viewer(harness, viewer):
    h = harness
    with TestClient(h.app()) as client:
        response = request(client, h, viewer)
    assert response.status_code == (
        404 if viewer in ("outsider", "other_campaign") else 200
    )
    assert (h.rows["note_private"].markdown[:200] in response.text) == (
        viewer == "player_a"
    )
    assert (h.rows["note_private"].id in response.text) == (viewer == "player_a")


@pytest.mark.parametrize("viewer", ("player_a", "player_b"))
def test_dm_narration_never_reaches_players(harness, viewer):
    h = harness
    with TestClient(h.app()) as client:
        response = request(client, h, viewer)
    assert response.status_code == 200
    assert h.rows["event_dm"].body["secret"] not in response.text
    assert h.rows["event_dm"].id not in response.text


@pytest.mark.parametrize("viewer", ("player_a", "player_b", "dm"))
def test_b_only_reveal_has_authorized_positive_control(harness, viewer):
    h = harness
    with TestClient(h.app()) as client:
        response = request(client, h, viewer)
    assert response.status_code == 200
    token = h.rows["reveal_b"].body["reveals"][0]["text"]
    assert (token in response.text) == (viewer in ("player_b", "dm"))


def test_partial_grant_and_reveal_return_only_revealed_details(harness):
    h = harness
    with TestClient(h.app()) as client:
        response = request(client, h, "player_a")
    results = {row["id"]: row for row in response.json()}
    entity = results[h.rows["partial"].id]
    assert entity["revealed_details"] == h.rows["grant_partial"].revealed_details
    assert "detail" not in entity and "source_book" not in entity
    assert h.rows["partial"].detail["secret"] not in response.text
    assert h.rows["partial_detail"].description not in response.text
    assert (
        h.rows["grant_partial"].revealed_details["secret"]
        in results[h.rows["reveal_partial"].id]["preview"]
    )


def test_name_only_identity_and_reveal_text_are_excluded_from_retrieval(harness):
    h = harness
    with TestClient(h.app()) as client:
        response = request(client, h, "player_a")
    assert response.status_code == 200
    assert h.rows["name_only"].id not in response.text
    assert h.rows["name_only"].name not in response.text
    assert h.rows["reveal_name_only"].id not in response.text
    assert h.rows["reveal_name_only"].body["reveals"][0]["text"] not in response.text


@pytest.mark.parametrize("grouped", (False, True))
def test_name_only_feed_preserves_recognition_without_text(harness, grouped):
    h = harness
    row = h.rows["reveal_name_only"]
    token = row.body["reveals"][0]["text"]
    if not grouped:
        row.body = row.body["reveals"][0]
        h.session.commit()
    with TestClient(h.app()) as client:
        response = client.get(
            f"/api/grimoire/campaigns/{h.rows['campaign'].id}/sessions/{row.session_id}/events",
            headers=h.headers("player_a"),
        )
    assert response.status_code == 200
    h.assert_no_leak(response, "player_a")
    assert h.rows["name_only"].name in response.text
    assert token not in response.text


@pytest.mark.parametrize("viewer", ROLES)
def test_deleted_retracted_and_foreign_sources_never_return(harness, viewer):
    h = harness
    with TestClient(h.app()) as client:
        response = request(client, h, viewer)
    assert response.status_code == (
        404 if viewer in ("outsider", "other_campaign") else 200
    )
    for key in (
        "note_deleted_character",
        "note_deleted_party",
        "note_foreign",
        "event_foreign",
        "fact_foreign",
        "fact_retracted",
        "foreign",
    ):
        assert h.rows[key].id not in response.text
    for key, row in h.rows.items():
        if isinstance(row, SessionEvent) and row.retracted_at is not None:
            assert row.id not in response.text, key
            assert row.body["secret"] not in response.text


def test_live_note_and_event_audience_changes_override_stale_copies(harness):
    h = harness
    note, event = h.rows["note_shared"], h.rows["event_table"]
    note.dm_readable = False
    event.audience = "dm"
    h.session.commit()
    assert h.rows["embedding_note_shared"].dm_readable is True
    assert h.rows["embedding_event_table"].audience == "table"
    with TestClient(h.app()) as client:
        assert note.id not in request(client, h, "dm").text
        assert event.id not in request(client, h, "player_a").text


@pytest.mark.parametrize(
    "viewer,key",
    [
        ("dm", "member_dm"),
        ("player_a", "member_player_a"),
        ("player_b", "member"),
        ("no_character", "member_no_character"),
    ],
)
def test_sqlite_knn_applies_candidate_predicate_independently_of_recheck(
    harness, monkeypatch, viewer, key
):
    h = harness
    candidates = []

    def sqlite_knn(session, query_vector, kinds, limit, model=None, where=None):
        assert where is not None
        rows = session.exec(
            select(Embedding).where(Embedding.embeddable_kind.in_(kinds), where)
        ).all()
        candidates.extend(row.embeddable_id for row in rows)
        return [(row, 0.0) for row in rows][:limit]

    monkeypatch.setattr(search, "knn_embeddings", sqlite_knn)
    with TestClient(h.app()) as client:
        response = request(client, h, viewer)
    assert response.status_code == 200
    member = h.rows[key]
    expected = {
        h.rows[f"embedding_{entity}"].embeddable_id
        for entity in ("private", "a_only", "b_only", "partial", "name_only", "foreign")
    }
    expected.add(h.rows["chunk_private"].id)
    for source in h.rows.values():
        if (
            isinstance(source, SessionEvent)
            and source.campaign_id == h.rows["campaign"].id
        ) and (
            viewer == "dm"
            or source.audience == "table"
            or (
                viewer != "no_character"
                and (
                    member.player_character_id in source.audience_pc_ids
                    or source.author_member_id == member.id
                )
            )
        ):
            expected.add(source.id)
    for note_key in (
        "note_private",
        "note_shared",
        "note_party",
        "note_deleted_character",
        "note_deleted_party",
    ):
        note = h.rows[note_key]
        if (note.kind == "party" and viewer != "no_character") or (
            note.kind == "character"
            and (
                note.dm_readable
                if viewer == "dm"
                else note.author_member_id == member.id
            )
        ):
            expected.add(note.id)
    for fact in h.rows.values():
        if (
            isinstance(fact, CharacterFact)
            and fact.campaign_id == h.rows["campaign"].id
            and viewer != "no_character"
            and (
                fact.viewer_key == "party"
                or viewer == "dm"
                or fact.player_character_id == member.player_character_id
            )
        ):
            expected.add(fact.id)
    # Compare pre-resolution ids. The live-source check cannot conceal a widened
    # SQL predicate, including foreign rows or DM-readable private-note copies.
    assert set(candidates) == expected


def test_types_sources_sorting_limit_and_live_note_preview(harness, monkeypatch):
    h = harness
    note = h.rows["note_private"]
    note.markdown = "Fresh live markdown " * 20
    h.session.commit()
    calls = []

    def ranked_knn(session, vector, kinds, limit, model=None, where=None):
        calls.append((kinds, limit, where))
        return [
            (row, index / 100)
            for index, (row, _) in enumerate(fake_knn(session, vector, kinds, limit))
        ][::-1]

    monkeypatch.setattr(search, "knn_embeddings", ranked_knn)
    with TestClient(h.app()) as client:
        result = request(client, h, "player_a").json()
        limited = request(client, h, "player_a", k=2).json()
    assert {row["type"] for row in result} == {
        "entity",
        "note",
        "event",
        "chunk",
        "fact",
    }
    assert result == sorted(result, key=lambda row: row["score"], reverse=True)
    assert len(limited) == 2 and limited == result[:2]
    assert len(calls) == 2 and calls[1][1] == 2 * search.OVERFETCH_FACTOR
    for row in result:
        expected = {"entity": {"entity_id": row["id"]}, "note": {"note_id": row["id"]}}
        if row["type"] == "event":
            assert row["source"] == {"session_id": row["session_id"], "seq": row["seq"]}
        elif row["type"] == "chunk":
            assert row["source"] == {"book_id": row["book_id"], "chunk_id": row["id"]}
        elif row["type"] == "fact":
            assert row["source"] == {
                "fact_id": row["id"],
                "session_id": h.rows["campaign_session"].id,
            }
            assert row["status"] in ("active", "disputed")
        else:
            assert row["source"] == expected[row["type"]]
    assert (
        next(row for row in result if row["id"] == note.id)["preview"]
        == note.markdown[:200]
    )


@pytest.mark.parametrize("viewer,key", (("player_a", "fact_a"), ("player_b", "fact_b")))
def test_search_returns_own_and_party_facts_but_never_another_pc(harness, viewer, key):
    h = harness
    with TestClient(h.app()) as client:
        response = request(client, h, viewer)
    facts = {row["id"]: row for row in response.json() if row["type"] == "fact"}
    assert h.rows[key].id in facts and h.rows["fact_party"].id in facts
    if viewer == "player_a":
        assert facts[h.rows["fact_disputed"].id]["status"] == "disputed"
        assert h.rows["fact_b"].id not in facts
    else:
        assert h.rows["fact_a"].id not in facts


def test_live_fact_owner_and_status_override_forged_embedding_copies(harness):
    h = harness
    fact = h.rows["fact_a"]
    fact.viewer_key = h.rows["character"].id
    fact.player_character_id = fact.viewer_key
    h.rows["fact_party"].status = "retracted"
    h.session.commit()
    assert h.rows["embedding_fact_a"].audience_pc_ids == [h.rows["character_a"].id]
    with TestClient(h.app()) as client:
        response = request(client, h, "player_a")
    assert fact.id not in response.text
    assert h.rows["fact_party"].id not in response.text


@pytest.mark.parametrize("params", [{"q": ""}, {"q": "x" * 201}, {"k": 0}, {"k": 51}])
def test_query_and_limit_validation(harness, params):
    with TestClient(harness.app()) as client:
        assert request(client, harness, "player_a", **params).status_code == 422


def test_knowledge_search_is_play_gated(harness, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_PLAY_ENABLED", "false")
    with TestClient(harness.app()) as client:
        assert request(client, harness, "player_a").status_code == 404


def test_knn_seam_composes_candidate_clause_and_model_before_limit():
    class Recorder:
        def execute(self, stmt):
            self.statement = stmt
            return self

        def all(self):
            return []

    recorder = Recorder()
    campaign_id = str(uuid4())
    assert (
        search.knn_embeddings(
            recorder,
            [0.0] * 1024,
            ("note", "event"),
            17,
            model="test-model",
            where=Embedding.campaign_id == campaign_id,
        )
        == []
    )
    compiled = recorder.statement.compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "embedding.campaign_id =" in sql
    assert "embedding.model =" in sql
    assert "ORDER BY" in sql and "LIMIT" in sql
    assert campaign_id in compiled.params.values()
    assert "test-model" in compiled.params.values()
    assert 17 in compiled.params.values()


@pytest.mark.parametrize(
    "kind,body,retracted,embedding_kind,visible",
    [
        ("utterance", {"text": "In character"}, False, "transcript", True),
        (
            "utterance",
            {"text": "Out of character", "ooc": True},
            False,
            "transcript",
            False,
        ),
        ("utterance", {"text": "Retracted"}, True, "transcript", False),
        ("action", {"text": "Private action"}, False, "event", False),
        ("handout", {"text": "Shared handout"}, False, "event", True),
        ("narration", {}, False, "event", False),
        ("utterance", {"text": "Wrong embedding kind"}, False, "event", False),
    ],
)
def test_only_eligible_live_events_and_transcripts_return(
    harness, kind, body, retracted, embedding_kind, visible
):
    h = harness
    row = SessionEvent(
        id=str(uuid4()),
        campaign_id=h.rows["campaign"].id,
        session_id=h.rows["campaign_session"].id,
        seq=90,
        kind=kind,
        audience="table",
        body=body,
        retracted_at=datetime.now(UTC) if retracted else None,
    )
    vector = Embedding(
        embeddable_kind=embedding_kind,
        embeddable_id=row.id,
        model="test",
        dim=1024,
        vector=[0.0] * 1024,
        **audience_columns(row),
    )
    h.session.add_all([row, vector])
    h.session.commit()
    with TestClient(h.app()) as client:
        result = request(client, h, "player_a").json()
    assert (row.id in {hit["id"] for hit in result}) == visible


def test_missing_sources_and_revoked_grants_fail_closed(harness):
    h = harness
    h.session.delete(h.rows["grant_a_only"])
    h.rows["embedding_note_shared"].embeddable_id = str(uuid4())
    h.rows["embedding_event_table"].embeddable_id = str(uuid4())
    h.session.commit()
    with TestClient(h.app()) as client:
        response = request(client, h, "player_a")
    assert response.status_code == 200
    for key in ("a_only", "note_shared", "event_table"):
        assert h.rows[key].id not in response.text


def test_grouped_reveal_drops_retracted_and_name_only_items(harness):
    h = harness
    row = h.rows["reveal_partial"]
    row.body = {
        "reveals": [
            row.body["reveals"][0],
            h.rows["reveal_name_only"].body["reveals"][0],
            h.rows["reveal_b"].body["reveals"][0],
        ],
        "retracted_entity_ids": [h.rows["b_only"].id],
    }
    h.session.commit()
    member = h.rows["member_player_a"]
    result = asyncio.run(
        search.search_knowledge(
            h.session,
            FakeEmbedClient(),
            h.rows["campaign"].id,
            member.player_character_id,
            member,
            "knowledge",
            50,
        )
    )
    hit = next(hit for hit in result if hit["id"] == row.id)
    assert h.rows["grant_partial"].revealed_details["secret"] in hit["preview"]
    assert h.rows["name_only"].name not in hit["preview"]
    assert h.rows["reveal_b"].body["reveals"][0]["text"] not in hit["preview"]
