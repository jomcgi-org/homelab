"""Tests for shared knowledge-pipeline constants and slug normalization."""

from knowledge.gardener import GARDENER_VERSION, MAX_GARDENER_RETRIES, _slugify


class TestSlugify:
    def test_ascii_text(self):
        assert _slugify("Hello World") == "hello-world"

    def test_unicode_nfkd_strips_accents(self):
        assert _slugify("Héllo") == "hello"

    def test_empty_string_returns_note(self):
        assert _slugify("") == "note"

    def test_multiple_special_chars_collapse_to_single_hyphen(self):
        assert _slugify("Hello!! World") == "hello-world"


class TestSurvivingConstants:
    def test_gardener_version_stamp(self):
        assert GARDENER_VERSION == "claude-sonnet-4-6@v1"

    def test_max_retries_ceiling(self):
        assert MAX_GARDENER_RETRIES == 3


def test_clone_merge_dry_run_provenance_and_idempotence(tmp_path):
    from sqlmodel import Session, SQLModel, create_engine, select
    from knowledge.gardener import merge_clones
    from knowledge.models import (
        AtomRawProvenance,
        Chunk,
        Dispute,
        Note,
        NoteLink,
        RawInput,
    )
    from knowledge.store import provenance_for_notes

    engine = create_engine(f"sqlite:///{tmp_path / 'clones.db'}").execution_options(
        schema_translate_map={"knowledge": None}
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            Note.__table__,
            Chunk.__table__,
            RawInput.__table__,
            AtomRawProvenance.__table__,
            Dispute.__table__,
            NoteLink.__table__,
        ],
    )
    with Session(engine) as session:
        raw = RawInput(raw_id="raw", path="raw", content_hash="raw", source="test")
        session.add(raw)
        session.flush()
        for key, confidence, scope, state in [
            ("winner", 0.9, "repo:acme/repo", "verified"),
            ("loser", 0.8, "repo:acme/repo", "verified"),
            ("unverified", 1, "repo:acme/repo", "unverified"),
            ("other", 1, "repo:other/repo", "verified"),
            ("disputed", 1, "repo:acme/repo", "disputed"),
            ("invalid", 1, "repo:acme/repo", "invalidated"),
        ]:
            note = Note(
                note_id=key,
                path=key,
                title=key,
                content_hash=key,
                type="fact",
                scope=scope,
                confidence=confidence,
                verification_state=state,
            )
            session.add(note)
            session.flush()
            session.add(
                Chunk(
                    note_fk=note.id,
                    chunk_index=0,
                    chunk_text=key,
                    embedding=[1.0] + [0.0] * 1023,
                )
            )
            session.add(
                AtomRawProvenance(
                    atom_fk=note.id,
                    raw_fk=raw.id,
                    derived_note_id=key,
                    gardener_version="original",
                )
            )
        session.commit()
        plan = merge_clones(session)
        assert plan == [{"survivor": "winner", "invalidated": ["loser", "unverified"]}]
        loser = session.exec(select(Note).where(Note.note_id == "loser")).one()
        assert loser.verification_state == "verified"
        assert len(session.exec(select(AtomRawProvenance)).all()) == 6
        assert merge_clones(session, apply=True) == plan
        assert loser.verification_state == "invalidated"
        assert loser.extra["merged_into"] == "winner"
        assert len(session.exec(select(AtomRawProvenance)).all()) == 8
        assert len(provenance_for_notes(session, ["winner"])["winner"]) == 3
        assert merge_clones(session, apply=True) == []
        assert len(session.exec(select(AtomRawProvenance)).all()) == 8


def test_multichunk_facts_require_full_coverage(tmp_path):
    from sqlmodel import Session, SQLModel, create_engine
    from knowledge.gardener import merge_clones
    from knowledge.models import Chunk, Dispute, Note

    engine = create_engine(f"sqlite:///{tmp_path / 'multichunk.db'}").execution_options(
        schema_translate_map={"knowledge": None}
    )
    SQLModel.metadata.create_all(
        engine, tables=[Note.__table__, Chunk.__table__, Dispute.__table__]
    )
    with Session(engine) as session:
        for key, vectors, confidence in [
            ("complete", [[1.0, 0.0], [0.0, 1.0]], 0.9),
            ("clone", [[0.0, 1.0], [1.0, 0.0]], 0.8),
            ("partial", [[1.0, 0.0]], 1.0),
        ]:
            note = Note(
                note_id=key,
                path=key,
                title=key,
                content_hash=key,
                type="fact",
                scope="repo:acme/repo",
                confidence=confidence,
                verification_state="verified",
            )
            session.add(note)
            session.flush()
            for i, vector in enumerate(vectors):
                session.add(
                    Chunk(
                        note_fk=note.id,
                        chunk_index=i,
                        chunk_text=key,
                        embedding=vector + [0.0] * 1022,
                    )
                )
        session.commit()
        assert merge_clones(session) == [
            {"survivor": "complete", "invalidated": ["clone"]}
        ]
