"""Luna-backed extraction of durable atoms from selected raw inputs."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text, update
from sqlmodel import Session, select

from knowledge import raw_store
from knowledge.models import AtomRawProvenance, Dispute, Note, RawInput
from shared.embedding import EmbeddingClient

KG_JOB_KIND = "kg-drain"
KG_NODE_KEY = "kg-drain"
GARDENER_VERSION = "kg-drain/luna@v1"
EXTRACTABLE_SOURCES = {
    "claude-session",
    "codex-session",
    "ember-session",
    "agent-report",
    "dispute",
}
RAW_BODY_CAP = 60_000
RELATED_NOTES = 8

_SCOPE_PATTERN = r"^(personal|org|repo|environment|session):.+$"
_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


class ExtractionInputMissing(ValueError):
    """The raw row or its object-store body is unavailable."""


class ExtractionOutputInvalid(ValueError):
    """The worker output does not satisfy the extraction contract."""


class _Edges(BaseModel):
    model_config = ConfigDict(extra="ignore")

    supersedes: list[str] = Field(default_factory=list)
    contradicts: list[str] = Field(default_factory=list)
    related: list[str] = Field(default_factory=list)


class _Assertion(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str
    body: str
    scope: str = Field(pattern=_SCOPE_PATTERN)
    verification_state: Literal["verified", "unverified"]
    confidence: float
    valid_from: str | None = None
    observed_at: str | None = None
    tags: list[str] = Field(default_factory=list)
    edges: _Edges = Field(default_factory=_Edges)
    evidence: list[str] = Field(default_factory=list)

    @field_validator("title", "body")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("body")
    @classmethod
    def _cap_body(cls, value: str) -> str:
        return value[:20_000]

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, value: object) -> float:
        number = float(value)
        return max(0.0, min(1.0, number))

    @field_validator("valid_from", "observed_at")
    @classmethod
    def _iso_timestamp(cls, value: str | None) -> str | None:
        if value is None:
            return None
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value


class _DisputeResolution(BaseModel):
    model_config = ConfigDict(extra="ignore")

    state: Literal["confirmed", "narrowed", "superseded", "invalidated", "rejected"]
    rationale: str


class _ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    assertions: list[_Assertion] = Field(default_factory=list, max_length=20)
    dispute_resolution: _DisputeResolution | None = None
    notes: str = ""


def enqueue_extraction(session: Session, raw_id: str) -> bool:
    """Register a one-shot extraction job, returning False if it already exists."""
    payload = json.dumps({"raw_id": raw_id})
    dialect = session.get_bind().dialect.name
    table = "routine_jobs" if dialect == "sqlite" else "claude_agent.routine_jobs"
    payload_expr = ":payload" if dialect == "sqlite" else "CAST(:payload AS JSONB)"
    sql = text(
        f"""
        INSERT INTO {table}
            (name, routine_kind, interval_secs, next_run_at, payload, created_by)
        VALUES
            (:name, :kind, NULL, CURRENT_TIMESTAMP, {payload_expr}, :created_by)
        ON CONFLICT (name) DO NOTHING
        """
    )
    result = session.execute(
        sql,
        {
            "name": f"kg:{raw_id}",
            "kind": KG_JOB_KIND,
            "payload": payload,
            "created_by": "knowledge.extraction",
        },
    )
    session.commit()
    return result.rowcount > 0


def _capped_body(body: str) -> str:
    if len(body) <= RAW_BODY_CAP:
        return body
    elided = len(body) - RAW_BODY_CAP
    while True:
        marker = f"\n[... {elided} chars elided ...]\n"
        kept = RAW_BODY_CAP - len(marker)
        updated = len(body) - kept
        if updated == elided:
            break
        elided = updated
    left = kept // 2
    right = kept - left
    return f"{body[:left]}{marker}{body[-right:]}"


def _lens(raw: RawInput) -> str:
    if raw.source in {"claude-session", "codex-session", "ember-session"}:
        return (
            "What did the agent learn that is reusable beyond this session? "
            "Separate facts confirmed by tool output (verification_state verified) "
            "from testimony (unverified). Prefer repository facts (scope repo:<repo "
            "from extra>). Skip anything already covered by the related notes unless "
            "it supersedes or contradicts them."
        )
    if raw.source == "agent-report":
        return (
            "This is a claim by an agent. State what it claims, what evidence the "
            "report cites, and whether the checkout confirms it. Emit verified only "
            "when the checkout confirms it; otherwise unverified with a confidence."
        )
    note_id = (raw.extra or {}).get("note_id", "<unknown>")
    return (
        f"An agent disputes note {note_id}. Seek disconfirming AND confirming "
        "evidence in the checkout. Return `dispute_resolution` with state in "
        "confirmed|narrowed|superseded|invalidated|rejected and a rationale, plus "
        "any replacement assertion."
    )


def build_extraction_prompt(session: Session, raw: RawInput) -> str:
    """Build the grounded extraction prompt for one raw input."""
    body = raw_store.fetch_raw(raw.content_hash)
    if not body:
        raise ExtractionInputMissing(f"raw content missing: {raw.raw_id}")
    body = _capped_body(body)
    vector = asyncio.run(EmbeddingClient().embed(body[:2000]))
    from knowledge.store import KnowledgeStore

    related = KnowledgeStore(session).search_notes_with_context(
        vector, limit=RELATED_NOTES
    )
    related_text = (
        "\n".join(
            "- [{note_id}] {title} ({scope}, {verification_state}): {snippet}".format(
                note_id=item.get("note_id", ""),
                title=item.get("title", ""),
                scope=item.get("scope") or "scope unknown",
                verification_state=item.get("verification_state") or "legacy",
                snippet=item.get("snippet", ""),
            )
            for item in related
        )
        or "- none"
    )
    output_contract = (
        "reply with exactly one fenced ```json block, last thing in the message, shaped "
        '{"assertions": [{"title", "body", "scope", "verification_state": '
        '"verified|unverified", "confidence": 0..1, "valid_from": iso|null, '
        '"observed_at": iso|null, "tags": [], "edges": {"supersedes": [note_id], '
        '"contradicts": [note_id], "related": [note_id]}, "evidence": '
        '["short pointers"]}], "dispute_resolution": {"state", "rationale"} | '
        'null, "notes": "free text"}; an empty `assertions` list is a valid answer.'
    )
    return (
        "You are the knowledge gardener. Extract durable, atomic assertions from the "
        "raw input below. You have the repo checkout at /workspace/src and may grep "
        "it to verify claims.\n\n"
        f"Source: {raw.source}\nExtra: {json.dumps(raw.extra or {}, sort_keys=True)}\n"
        f"Lens: {_lens(raw)}\n\nRelated notes:\n{related_text}\n\n"
        f"Raw input:\n{body}\n\nOutput contract: {output_contract}"
    )


def _record_failure(session: Session, raw: RawInput, error: str) -> None:
    existing = session.exec(
        select(AtomRawProvenance).where(
            AtomRawProvenance.raw_fk == raw.id,
            AtomRawProvenance.derived_note_id == "failed",
        )
    ).first()
    if existing is None:
        session.add(
            AtomRawProvenance(
                raw_fk=raw.id,
                derived_note_id="failed",
                gardener_version=GARDENER_VERSION,
                error=error[:500],
                retry_count=1,
            )
        )
    else:
        existing.retry_count += 1
        existing.error = error[:500]
        existing.gardener_version = GARDENER_VERSION
        session.add(existing)
    session.commit()


def _parse_result(result_text: str) -> _ExtractionResult:
    blocks = _JSON_BLOCK_RE.findall(result_text)
    if not blocks:
        raise ExtractionOutputInvalid("missing fenced json block")
    try:
        payload = json.loads(blocks[-1])
        return _ExtractionResult.model_validate(payload)
    except ExtractionOutputInvalid:
        raise
    except Exception as exc:
        raise ExtractionOutputInvalid(str(exc)) from exc


def apply_extraction(session: Session, raw_id: str, result_text: str) -> dict:
    """Validate worker output and atomically apply its knowledge changes."""
    raw = session.exec(select(RawInput).where(RawInput.raw_id == raw_id)).first()
    if raw is None:
        raise ExtractionInputMissing(f"raw not found: {raw_id}")
    try:
        parsed = _parse_result(result_text)
    except ExtractionOutputInvalid as exc:
        _record_failure(session, raw, str(exc))
        raise

    from knowledge.atoms import index_atom

    note_ids: list[str] = []
    try:
        for assertion in parsed.assertions:
            note_id = asyncio.run(
                index_atom(
                    session,
                    title=assertion.title,
                    body=assertion.body,
                    type="fact",
                    visibility="private",
                    source_tier=raw.source,
                    tags=assertion.tags,
                    edges=assertion.edges.model_dump(),
                    derived_from_raw=raw_id,
                    scope=assertion.scope,
                    verification_state=assertion.verification_state,
                    confidence=assertion.confidence,
                    valid_from=assertion.valid_from,
                    observed_at=assertion.observed_at,
                    commit=False,
                )
            )
            note = session.exec(select(Note).where(Note.note_id == note_id)).one()
            session.add(
                AtomRawProvenance(
                    atom_fk=note.id,
                    raw_fk=raw.id,
                    derived_note_id=note_id,
                    gardener_version=GARDENER_VERSION,
                )
            )
            note_ids.append(note_id)

        if not note_ids:
            session.add(
                AtomRawProvenance(
                    raw_fk=raw.id,
                    derived_note_id="no-new-notes",
                    gardener_version=GARDENER_VERSION,
                )
            )

        dispute_state = None
        resolution = parsed.dispute_resolution
        disputed_note_id = (raw.extra or {}).get("note_id")
        if resolution is not None and isinstance(disputed_note_id, str):
            dispute_state = resolution.state
            now = datetime.now(timezone.utc)
            session.exec(
                update(Dispute)
                .where(Dispute.note_id == disputed_note_id, Dispute.state == "open")
                .values(
                    state=resolution.state,
                    resolution=resolution.rationale,
                    resolved_at=now,
                )
            )
            note_state = {
                "confirmed": "disputed",
                "invalidated": "invalidated",
                "rejected": "verified",
            }.get(resolution.state)
            if note_state is not None:
                session.exec(
                    update(Note)
                    .where(Note.note_id == disputed_note_id, Note.deleted_at.is_(None))
                    .values(verification_state=note_state)
                )
        session.commit()
    except Exception:
        session.rollback()
        raise

    return {
        "raw_id": raw_id,
        "atoms": note_ids,
        "dispute": dispute_state,
        "failed": False,
    }
