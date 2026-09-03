"""Server-side quality gate coverage for the kg-drain lane."""

from __future__ import annotations

import json

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from knowledge.extraction import EXTRACTION_VERSION, apply_extraction
from knowledge.models import AtomRawProvenance, Note, RawInput


class _Embedder:
    async def embed(self, _text):
        return [0.0] * 1024

    async def embed_batch(self, texts):
        return [[0.0] * 1024 for _ in texts]


@pytest.fixture(name="session")
def session_fixture(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'extraction-gates.db'}")
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    monkeypatch.setattr("knowledge.extraction.EmbeddingClient", _Embedder)
    monkeypatch.setattr("knowledge.atoms.EmbeddingClient", _Embedder)
    monkeypatch.setattr(
        "knowledge.store.KnowledgeStore.search_notes_with_context",
        lambda *_args, **_kwargs: [],
    )
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as db_session:
            yield db_session
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


def _raw(session: Session) -> RawInput:
    count = len(session.exec(select(RawInput)).all())
    raw = RawInput(
        raw_id=f"gate-raw-{count}",
        path=f"raws/gate-raw-{count}.md",
        source="codex-session",
        content_hash=f"gate-hash-{count}",
    )
    session.add(raw)
    session.commit()
    session.refresh(raw)
    return raw


def _assertion(title: str, body: str, **overrides) -> dict:
    assertion = {
        "title": title,
        "body": body,
        "scope": "repo:jomcgi-org/homelab",
        "verification_state": "unverified",
        "confidence": 0.8,
        "evidence": ["projects/monolith/knowledge/extraction.py:1"],
    }
    assertion.update(overrides)
    return assertion


def _result(*assertions: dict) -> str:
    return "```json\n" + json.dumps({"assertions": list(assertions)}) + "\n```"


def test_event_gate_rejects_graded_session_event(session):
    raw = _raw(session)

    result = apply_extraction(
        session,
        raw.raw_id,
        _result(
            _assertion(
                "qwen-monolith-session-32-was-accepted",
                "The session was accepted by the control plane.",
            )
        ),
    )

    assert result["atoms"] == []
    assert [item["reason_code"] for item in result["rejected"]] == ["event"]


def test_event_gate_keeps_system_behavior(session):
    raw = _raw(session)

    result = apply_extraction(
        session,
        raw.raw_id,
        _result(
            _assertion(
                "session-recovery-clears-stale-bindings",
                "When recovery finds a stale binding, it clears the binding so that "
                "later work can acquire capacity.",
            )
        ),
    )

    assert result["rejected"] == []
    assert result["atoms"] == ["session-recovery-clears-stale-bindings"]


def test_value_gate_rejects_graded_bare_value(session):
    raw = _raw(session)

    result = apply_extraction(
        session,
        raw.raw_id,
        _result(_assertion("freetoken-is-apache-2-0", "Freetoken is Apache-2-0.")),
    )

    assert result["atoms"] == []
    assert [item["reason_code"] for item in result["rejected"]] == ["value"]


def test_value_gate_keeps_graded_behavioral_fact(session):
    raw = _raw(session)

    result = apply_extraction(
        session,
        raw.raw_id,
        _result(
            _assertion(
                "kargo-promotion-state-is-authoritative-over-git-chart-version-for-live-deployment",
                "Kargo promotion state is authoritative over the Git chart version "
                "when determining the live deployment.",
            )
        ),
    )

    assert result["rejected"] == []
    assert len(result["atoms"]) == 1


def test_duplicate_gate_attaches_raw_to_existing_note(session, monkeypatch):
    existing = Note(
        note_id="qwen-is-a-deprecated-alias-for-the-spark-pi-model",
        path="_processed/qwen-alias.md",
        title="qwen-is-a-deprecated-alias-for-the-spark-pi-model",
        content_hash="existing-hash",
        content="Qwen resolves to Spark so persisted sessions retain Pi compatibility.",
        type="fact",
        scope="repo:jomcgi-org/homelab",
    )
    session.add(existing)
    session.commit()
    session.refresh(existing)
    monkeypatch.setattr(
        "knowledge.store.KnowledgeStore.search_notes_with_context",
        lambda _store, _vector, **kwargs: [
            {
                "note_id": str(existing.note_id),
                "score": 0.97,
                "scope": "repo:jomcgi-org/homelab",
            }
        ],
    )
    raw = _raw(session)

    result = apply_extraction(
        session,
        raw.raw_id,
        _result(
            _assertion(
                "qwen-is-a-deprecated-alias-for-the-spark-pi-model",
                "Qwen resolves to Spark because persisted sessions require Pi compatibility.",
            )
        ),
    )

    assert result["atoms"] == []
    assert [item["reason_code"] for item in result["rejected"]] == ["duplicate"]
    assert len(session.exec(select(Note)).all()) == 1
    provenance = session.exec(
        select(AtomRawProvenance).where(
            AtomRawProvenance.raw_fk == raw.id,
            AtomRawProvenance.derived_note_id == existing.note_id,
            AtomRawProvenance.gardener_version == EXTRACTION_VERSION,
        )
    ).one()
    assert provenance.atom_fk == existing.id


def test_supersession_wins_over_duplicate_gate(session, monkeypatch):
    existing = Note(
        note_id="old-alias-rule",
        path="_processed/old-alias-rule.md",
        title="Old alias rule",
        content_hash="old-alias-hash",
        content="Qwen resolves to Spark so persisted sessions retain Pi compatibility.",
        type="fact",
        scope="repo:jomcgi-org/homelab",
    )
    session.add(existing)
    session.commit()
    monkeypatch.setattr(
        "knowledge.store.KnowledgeStore.search_notes_with_context",
        lambda *_args, **_kwargs: [{"note_id": "old-alias-rule", "score": 0.99}],
    )
    raw = _raw(session)

    result = apply_extraction(
        session,
        raw.raw_id,
        _result(
            _assertion(
                "current-alias-rule",
                "Qwen now resolves to Spark because persisted sessions require Pi compatibility.",
                edges={"supersedes": ["old-alias-rule"]},
            )
        ),
    )

    assert result["rejected"] == []
    assert result["atoms"] == ["current-alias-rule"]


def test_duplicate_search_excludes_invalidated_notes(session, monkeypatch):
    search_kwargs = []
    monkeypatch.setattr(
        "knowledge.store.KnowledgeStore.search_notes_with_context",
        lambda _store, _vector, **kwargs: search_kwargs.append(kwargs) or [],
    )
    raw = _raw(session)

    result = apply_extraction(
        session,
        raw.raw_id,
        _result(
            _assertion(
                "capacity-deferral-behavior",
                "The scheduler defers work when capacity cannot be acquired.",
            )
        ),
    )

    assert result["rejected"] == []
    assert search_kwargs == [
        {
            "limit": 3,
            "scope_filter": "repo:jomcgi-org/homelab",
            "exclude_invalidated": True,
        }
    ]


def test_embeddings_finish_before_write_transaction_opens(session, monkeypatch):
    class _OutsideTransactionEmbedder:
        async def embed(self, _text):
            assert not session.in_transaction()
            return [0.0] * 1024

        async def embed_batch(self, texts):
            assert not session.in_transaction()
            return [[0.0] * 1024 for _ in texts]

    monkeypatch.setattr(
        "knowledge.extraction.EmbeddingClient", _OutsideTransactionEmbedder
    )
    raw = _raw(session)

    result = apply_extraction(
        session,
        raw.raw_id,
        _result(
            _assertion(
                "capacity-deferral-behavior",
                "The scheduler defers work when capacity cannot be acquired.",
            )
        ),
    )

    assert result["atoms"] == ["capacity-deferral-behavior"]


def test_unsupported_gate_rejects_high_confidence_without_evidence(session):
    raw = _raw(session)

    result = apply_extraction(
        session,
        raw.raw_id,
        _result(
            _assertion(
                "unsupported-high-confidence-claim",
                "The scheduler defers work when capacity cannot be acquired.",
                verification_state="verified",
                confidence=0.95,
                evidence=[],
            )
        ),
    )

    assert result["atoms"] == []
    assert [item["reason_code"] for item in result["rejected"]] == ["unsupported"]
    session.refresh(raw)
    assert raw.extra["extraction_passes"] == 1
    assert raw.extra["extraction_rejected"] == result["rejected"]


def test_verified_tool_output_without_grounding_is_downgraded(session):
    raw = _raw(session)

    result = apply_extraction(
        session,
        raw.raw_id,
        _result(
            _assertion(
                "capacity-deferral-behavior",
                "The scheduler defers work when capacity cannot be acquired.",
                verification_state="verified",
                confidence=0.8,
                evidence=["operator observation"],
            )
        ),
    )

    note = session.exec(select(Note).where(Note.note_id == result["atoms"][0])).one()
    assert note.verification_state == "unverified"


def test_only_tool_output_evidence_is_an_event(session):
    raw = _raw(session)

    result = apply_extraction(
        session,
        raw.raw_id,
        _result(
            _assertion(
                "capacity-observation",
                "Capacity pressure delayed later work.",
                evidence=["tool output: status=deferred"],
            )
        ),
    )

    assert [item["reason_code"] for item in result["rejected"]] == ["event"]


def test_correction_pass_is_allowed_once_and_appends_rejections(session):
    raw = _raw(session)
    first = apply_extraction(
        session,
        raw.raw_id,
        _result(_assertion("freetoken-is-apache-2-0", "Freetoken is Apache-2-0.")),
    )
    second = apply_extraction(
        session,
        raw.raw_id,
        _result(
            _assertion(
                "capacity-deferral-behavior",
                "The scheduler defers work when capacity cannot be acquired.",
            )
        ),
        correction=True,
    )
    third = apply_extraction(
        session,
        raw.raw_id,
        _result(
            _assertion(
                "another-behavior",
                "The scheduler retries work when capacity becomes available.",
            )
        ),
        correction=True,
    )

    assert len(first["rejected"]) == 1
    assert second["atoms"] == ["capacity-deferral-behavior"]
    assert third["replayed"] is True
    session.refresh(raw)
    assert raw.extra["extraction_passes"] == 2
