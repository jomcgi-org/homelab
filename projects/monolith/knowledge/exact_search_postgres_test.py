"""Exact-token ranking and authorization against PostgreSQL 16 + pgvector."""

from datetime import datetime, timedelta, timezone
from math import sqrt
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, create_engine

from knowledge.entities import Entity, NoteEntity
from knowledge.freshness import STANDARD, VOLATILE
from knowledge.models import AtomRawProvenance, Chunk, Dispute, Note, RawInput
from knowledge.store import KnowledgeStore, _rank_search_chunks

_QUERY = [1.0] + [0.0] * 1023
_SCOPE = "repo:exact-search/test"


@pytest.fixture
def ranked_session(pg):
    # The pg fixture fails, rather than skips, if PostgreSQL cannot start.
    engine = create_engine(pg.url)
    with engine.connect() as connection:
        transaction = connection.begin()
        with Session(bind=connection) as session:
            session.info["now"] = datetime.now(timezone.utc)
            yield session
        transaction.rollback()
    engine.dispose()


def _note(session, name, score=0.9, *, chunk_text="ordinary evidence", **fields):
    identity = f"exact-{name}-{uuid4().hex}"
    now = session.info["now"]
    defaults = dict(
        note_id=identity,
        path=f"notes/{identity}.md",
        title=name,
        content_hash=identity,
        content="ordinary evidence",
        type="fact",
        scope=_SCOPE,
        verification_state="verified",
        observed_at=now - timedelta(days=2),
        review_after=now + timedelta(days=30),
        review_policy=STANDARD,
    )
    defaults.update(fields)
    note = Note(**defaults)
    session.add_all([note])
    session.flush()
    chunk = Chunk(
        note_fk=note.id,
        chunk_index=0,
        chunk_text=chunk_text + " filler" * 30,
        embedding=[score, sqrt(1 - score * score)] + [0.0] * 1022,
    )
    session.add_all([chunk])
    session.flush()
    return note


def _rank(session, query="#12", limit=20, **kwargs):
    return _rank_search_chunks(
        session,
        _QUERY,
        limit,
        kwargs.pop("type_filter", None),
        scope_filters=kwargs.pop("scope_filters", (_SCOPE,)),
        query_text=query,
        **kwargs,
    )


def test_identifier_tier_admits_low_score_and_preserves_returned_score(ranked_session):
    session = ranked_session
    semantic = _note(session, "semantic", 0.95)
    exact = _note(session, "exact #12", 0.1)
    rows = _rank(session)
    assert [row[0] for row in rows] == [exact.id, semantic.id]
    assert rows[0][2] == pytest.approx(0.1)
    results = KnowledgeStore(session).search_notes_with_context(
        _QUERY,
        scope_filters=(_SCOPE,),
        query_text="#12",
    )
    assert [result["note_id"] for result in results] == [
        exact.note_id,
        semantic.note_id,
    ]
    assert results[0]["score"] == pytest.approx(0.1)


def test_identifier_boundaries_do_not_boost_longer_number(ranked_session):
    session = ranked_session
    longer = _note(session, "#123", 0.6)
    unrelated = _note(session, "semantic", 0.9)
    below = _note(session, "#123 below threshold", 0.1)
    assert [row[0] for row in _rank(session)] == [unrelated.id, longer.id]
    assert below.id not in [row[0] for row in _rank(session)]


@pytest.mark.parametrize(
    "field", ["title", "path", "content", "chunk", "raw", "legacy_raw"]
)
def test_path_matches_text_and_provenance_including_null_content(ranked_session, field):
    session = ranked_session
    token = "projects/a_b/store.py"
    fields = {field: f"`{token}`"} if field in {"title", "path", "content"} else {}
    if field == "chunk":
        fields.update(content=None, chunk_text=f"See `{token}`.")
    exact = _note(session, "exact", 0.1, **fields)
    if field in {"raw", "legacy_raw"}:
        raw = RawInput(
            raw_id=uuid4().hex,
            path=f"raw/{uuid4().hex}.md",
            source="test",
            original_path=token,
            content_hash="hash",
        )
        session.add_all([raw])
        session.flush()
        session.add_all(
            [
                AtomRawProvenance(
                    raw_fk=raw.id,
                    derived_note_id=exact.note_id if field == "raw" else None,
                    atom_fk=exact.id if field == "legacy_raw" else None,
                    gardener_version="test",
                )
            ]
        )
        session.flush()
    semantic = _note(session, "semantic", 0.95)
    assert [row[0] for row in _rank(session, token)] == [exact.id, semantic.id]


@pytest.mark.parametrize(
    "text", ["xa/b.md", "a/b.md.bak", "a/b.mdx", "aX/b.md", "a/b%20.md", "a/bXmd"]
)
def test_path_boundaries_and_regex_escaping(ranked_session, text):
    session = ranked_session
    _note(session, "near miss", 0.1, content=text)
    semantic = _note(session, "semantic", 0.9)
    assert [row[0] for row in _rank(session, "a/b.md")] == [semantic.id]


def test_underscore_path_does_not_match_other_characters(ranked_session):
    session = ranked_session
    _note(session, "near miss", 0.1, content="projects/aXb/store.py")
    semantic = _note(session, "semantic", 0.9)
    assert [row[0] for row in _rank(session, "projects/a_b/store.py")] == [semantic.id]


def test_issue_entity_provenance(ranked_session):
    session = ranked_session
    exact = _note(session, "disposition", 0.1)
    entity = Entity(kind="issue", slug="12", title="Issue 12", source="test")
    session.add_all([entity])
    session.flush()
    session.add_all(
        [
            NoteEntity(
                note_id=exact.note_id,
                entity_id=entity.id,
                role="subject",
                source="test",
            )
        ]
    )
    session.flush()
    assert [row[0] for row in _rank(session)] == [exact.id]


def test_any_chunk_can_supply_exact_match_not_just_best_chunk(ranked_session):
    session = ranked_session
    exact = _note(session, "exact", 0.8, content=None)
    session.add_all(
        [
            Chunk(
                note_fk=exact.id,
                chunk_index=1,
                chunk_text="Evidence for #12" + " filler" * 30,
                embedding=[0.1, sqrt(0.99)] + [0.0] * 1022,
            )
        ]
    )
    session.flush()
    semantic = _note(session, "semantic", 0.95)
    rows = _rank(session)
    assert [row[0] for row in rows] == [exact.id, semantic.id]
    assert rows[0][2] == pytest.approx(0.8)


@pytest.mark.parametrize(
    "fields",
    [
        {"scope": "personal:someone-else"},
        {"verification_state": "legacy"},
        {"source": "deployment-observation"},
        {"deleted_at": datetime(2020, 1, 1, tzinfo=timezone.utc)},
        {"type": "paper"},
    ],
)
def test_filtered_exact_match_never_consumes_limit(ranked_session, fields):
    session = ranked_session
    _note(session, "unauthorized #12", 0.99, **fields)
    allowed = _note(session, "allowed", 0.8)
    assert [row[0] for row in _rank(session, limit=1, type_filter="fact")] == [
        allowed.id
    ]


def test_empty_scope_allow_list_fails_closed(ranked_session):
    _note(ranked_session, "#12", 0.99)
    assert _rank(ranked_session, scope_filters=()) == []


def test_exact_without_chunk_cannot_be_admitted(ranked_session):
    note = Note(
        note_id=uuid4().hex,
        path=f"notes/{uuid4().hex}",
        title="#12",
        content_hash="hash",
        scope=_SCOPE,
        verification_state="verified",
    )
    ranked_session.add_all([note])
    ranked_session.flush()
    assert _rank(ranked_session) == []


def test_current_then_recency_then_semantic_score(ranked_session):
    session = ranked_session
    now = datetime.now(timezone.utc)
    expired = _note(session, "expired #12", 0.99, valid_until=now - timedelta(days=1))
    invalid = _note(session, "invalid #12", 0.95, verification_state="invalidated")
    older = _note(session, "older #12", 0.9, valid_from=now - timedelta(days=3))
    newer = _note(
        session,
        "newer #12",
        0.6,
        observed_at=now - timedelta(days=1),
        valid_until=now + timedelta(days=1),
    )
    undated = _note(
        session,
        "undated #12",
        0.8,
        observed_at=None,
        last_reviewed_at=now - timedelta(days=2),
    )
    semantic = _note(session, "semantic", 0.999, valid_from=now)
    assert [row[0] for row in _rank(session)] == [
        newer.id,
        older.id,
        undated.id,
        expired.id,
        invalid.id,
        semantic.id,
    ]
    assert [row[0] for row in _rank(session, exclude_invalidated=True)] == [
        # A future valid_until is still current, so it is retained.
        newer.id,
        older.id,
        undated.id,
        semantic.id,
    ]


def test_no_token_query_keeps_semantic_order(ranked_session):
    session = ranked_session
    high = _note(session, "old #12", 0.95, verification_state="invalidated")
    low = _note(session, "new", 0.7, valid_from=datetime.now(timezone.utc))
    _note(session, "below #12", 0.1)
    expected = [high.id, low.id]
    assert [row[0] for row in _rank(session, None)] == expected
    assert [row[0] for row in _rank(session, "ordinary question")] == expected
    assert [row[0] for row in _rank(session, "#999")] == expected


def test_exact_score_ties_use_note_id(ranked_session):
    first = _note(ranked_session, "first #12", 0.9)
    second = _note(ranked_session, "second #12", 0.9)
    assert [row[0] for row in _rank(ranked_session)] == [first.id, second.id]


def test_temporal_predicate_history_scope_visibility_and_disputes(ranked_session):
    session = ranked_session
    now = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
    common = {"observed_at": now - timedelta(hours=24), "review_policy": VOLATILE}
    current = _note(
        session,
        "PR open #12",
        review_after=now + timedelta(seconds=1),
        visibility="private",
        **common,
    )
    due = _note(session, "PR checks passing #12", review_after=now, **common)
    unknown = _note(session, "unknown #12", review_after=None, observed_at=None)
    _note(
        session, "other repo #12", scope="repo:someone/else", review_after=now, **common
    )
    _note(
        session,
        "other personal #12",
        scope="personal:someone",
        review_after=now,
        **common,
    )
    _note(session, "unscoped #12", scope=None, review_after=now, **common)
    session.add_all(
        [Dispute(note_id=due.note_id, reason="still disputed", state="open")]
    )
    session.flush()
    store = KnowledgeStore(session, now=now)
    results = store.search_notes_with_context(
        _QUERY, query_text="#12", scope_filters=(_SCOPE,)
    )
    assert [row["note_id"] for row in results] == [current.note_id]
    assert results[0]["freshness"] == "current"
    assert results[0]["requires_authoritative_observation"] is True
    assert _rank(session, now=now, exclude_invalidated=True)[0][0] == current.id
    history = store.search_notes_with_context(
        _QUERY,
        query_text="#12",
        scope_filters=(_SCOPE,),
        include_history=True,
        exclude_invalidated=False,
    )
    assert {row["note_id"] for row in history} == {
        current.note_id,
        due.note_id,
        unknown.note_id,
    }
    dated = next(row for row in history if row["note_id"] == due.note_id)
    assert dated["freshness"] == "due"
    assert dated["disputed"] is True
    assert dated["review_after"] == now.isoformat()
    assert dated["observed_at"] == common["observed_at"].isoformat()
    assert store.get_note_by_id(due.note_id)["freshness"] == "due"
    at_boundary = KnowledgeStore(session, now=now + timedelta(seconds=1))
    assert at_boundary.search_notes_with_context(_QUERY, scope_filters=(_SCOPE,)) == []


def test_postgres_review_check_uses_evidence_basis_and_elapsed_hours(ranked_session):
    session = ranked_session
    basis = datetime(2026, 3, 8, 8, tzinfo=timezone.utc)
    _note(
        session, "max age", observed_at=basis, review_after=basis + timedelta(days=90)
    )
    _note(
        session,
        "reviewed historical",
        observed_at=basis - timedelta(days=500),
        last_reviewed_at=basis,
        review_after=basis + timedelta(days=90),
    )
    for fields in (
        {
            "observed_at": basis,
            "review_after": basis + timedelta(days=90, microseconds=1),
        },
        {"observed_at": None, "review_after": basis},
    ):
        with pytest.raises(IntegrityError, match="notes_review_deadline_chk"):
            with session.begin_nested():
                _note(session, "illegal lease", **fields)


_OTHER_SCOPE = "repo:other-scope/test"


def _scoped_ranking_fixture(session):
    # Every out-of-scope note outranks every in-scope note by similarity, so a
    # scope filter applied after the global top-k would leave nothing behind.
    outside = [
        _note(session, f"outside {score}", score, scope=_OTHER_SCOPE)
        for score in (0.95, 0.94, 0.93, 0.92)
    ]
    inside = [
        _note(session, f"inside {score}", score) for score in (0.80, 0.78, 0.76, 0.74)
    ]
    return outside, inside


def test_scoped_search_returns_top_k_within_scope_not_scoped_subset_of_global_top_k(
    ranked_session,
):
    session = ranked_session
    outside, inside = _scoped_ranking_fixture(session)
    store = KnowledgeStore(session)
    # Precondition: across both scopes the global top-3 is all out of scope.
    global_top = store.search_notes_with_context(
        _QUERY, limit=3, scope_filters=(_SCOPE, _OTHER_SCOPE)
    )
    assert [row["note_id"] for row in global_top] == [
        note.note_id for note in outside[:3]
    ]
    scoped = store.search_notes_with_context(_QUERY, limit=3, scope_filter=_SCOPE)
    assert [row["note_id"] for row in scoped] == [note.note_id for note in inside[:3]]
    assert [row["score"] for row in scoped] == pytest.approx([0.80, 0.78, 0.76])
    assert {row["scope"] for row in scoped} == {_SCOPE}


def test_scoped_rank_search_chunks_returns_top_k_within_scope(ranked_session):
    session = ranked_session
    outside, inside = _scoped_ranking_fixture(session)
    global_top = _rank_search_chunks(
        session, _QUERY, 3, None, scope_filters=(_SCOPE, _OTHER_SCOPE)
    )
    assert [row[0] for row in global_top] == [note.id for note in outside[:3]]
    scoped = _rank_search_chunks(session, _QUERY, 3, None, scope_filters=(_SCOPE,))
    assert [row[0] for row in scoped] == [note.id for note in inside[:3]]
    assert [row[2] for row in scoped] == pytest.approx([0.80, 0.78, 0.76])


def test_scoped_exact_token_search_returns_top_k_within_scope(ranked_session):
    session = ranked_session
    outside, inside = _scoped_ranking_fixture(session)
    # "#999" yields an identifier token that matches no note, so the exact-token
    # ordering is active but falls through to semantic score within scope.
    scoped = _rank(session, "#999", limit=3, scope_filters=(_SCOPE,))
    assert [row[0] for row in scoped] == [note.id for note in inside[:3]]
    both = _rank(session, "#999", limit=3, scope_filters=(_SCOPE, _OTHER_SCOPE))
    assert [row[0] for row in both] == [note.id for note in outside[:3]]
    results = KnowledgeStore(session).search_notes_with_context(
        _QUERY, limit=3, scope_filter=_SCOPE, query_text="#999"
    )
    assert [row["note_id"] for row in results] == [note.note_id for note in inside[:3]]
