"""Campaign voice presets and per-viewer narration speaker keys."""

import hashlib
import re
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy import func
from sqlmodel import Session, select

from grimoire.models import CampaignVoice, Entity
from grimoire.visibility import entity_belongs_to_campaign

VoiceName = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=64)]


class VoiceHint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lang: Annotated[str, StringConstraints(strict=True, max_length=35)] | None = None
    names: list[VoiceName] = Field(default_factory=list, max_length=8)


class VoiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    voice_hint: VoiceHint = Field(default_factory=VoiceHint)
    rate: float = Field(default=1, ge=0.5, le=2, allow_inf_nan=False)
    pitch: float = Field(default=1, ge=0, le=2, allow_inf_nan=False)


class VoiceView(VoiceRequest):
    speaker_key: str
    updated_at: datetime

    @field_validator("updated_at")
    @classmethod
    def timestamp_as_utc(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def validate_speaker_key(session: Session, campaign_id: str, key: object) -> str:
    """Validate and canonicalize a key after the caller's campaign access check."""
    if not isinstance(key, str) or key.startswith("ref:"):
        raise HTTPException(422, "invalid speaker key")
    try:
        entity_id = str(UUID(key))
    except ValueError:
        # Dot-only labels are rejected: "/voices/.." normalizes to another path.
        valid = re.fullmatch(r"[A-Za-z0-9 _'.-]{1,64}", key) is not None
        if not valid or not key.strip("."):
            raise HTTPException(422, "invalid speaker key") from None
        return key
    entity = session.exec(
        select(Entity).where(Entity.id.in_((entity_id, entity_id.upper())))
    ).first()
    if (
        entity is None
        or entity.entity_type != "npc"
        or not entity_belongs_to_campaign(session, campaign_id, entity)
    ):
        raise HTTPException(404, "NPC not found in campaign")
    return entity_id


def speaker_ref(campaign_id: str, key: str) -> str:
    """Hide entity identifiers while keeping voice rows and narration aligned."""
    try:
        entity_id = str(UUID(key))
    except ValueError:
        return key
    digest = hashlib.sha256(f"{UUID(campaign_id)}:{entity_id}".encode()).hexdigest()
    return "ref:" + digest[:20]


def voice_view(row: CampaignVoice, *, dm: bool) -> VoiceView:
    return VoiceView(
        speaker_key=row.speaker_key
        if dm
        else speaker_ref(row.campaign_id, row.speaker_key),
        voice_hint=row.voice_hint,
        rate=row.rate,
        pitch=row.pitch,
        updated_at=row.updated_at,
    )


def _find_voice(session: Session, campaign_id: str, key: str) -> CampaignVoice | None:
    matches_key = CampaignVoice.speaker_key == key
    try:
        UUID(key)
    except ValueError:
        pass
    else:
        matches_key = func.lower(CampaignVoice.speaker_key) == key
    return session.exec(
        select(CampaignVoice).where(
            CampaignVoice.campaign_id == campaign_id, matches_key
        )
    ).first()


def upsert_voice(
    session: Session, campaign_id: str, key: str, body: VoiceRequest
) -> CampaignVoice:
    key = validate_speaker_key(session, campaign_id, key)
    row = _find_voice(session, campaign_id, key)
    if row is None:
        row = CampaignVoice(campaign_id=campaign_id, speaker_key=key)
        session.add(row)
    row.speaker_key = key
    row.voice_hint = body.voice_hint.model_dump()
    row.rate = body.rate
    row.pitch = body.pitch
    row.updated_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(row)
    return row


def delete_voice(session: Session, campaign_id: str, key: str) -> None:
    key = validate_speaker_key(session, campaign_id, key)
    row = _find_voice(session, campaign_id, key)
    if row is None:
        raise HTTPException(404, "voice not found")
    session.delete(row)
    session.commit()
