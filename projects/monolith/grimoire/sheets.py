"""Bounded v1 character-sheet validation and server-owned derivation."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlmodel import Session

from grimoire.models import Entity
from grimoire.visibility import visible_entities_query

Ability = Literal[
    "strength",
    "dexterity",
    "constitution",
    "intelligence",
    "wisdom",
    "charisma",
]

ABILITIES: tuple[Ability, ...] = (
    "strength",
    "dexterity",
    "constitution",
    "intelligence",
    "wisdom",
    "charisma",
)

_HIT_DIE = re.compile(r"(?:^|\b)d(6|8|10|12)(?:\b|$)", re.IGNORECASE)


class SheetValidationError(ValueError):
    """The submitted base facts cannot produce a complete v1 sheet."""


class AbilityScores(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strength: int = Field(ge=3, le=20)
    dexterity: int = Field(ge=3, le=20)
    constitution: int = Field(ge=3, le=20)
    intelligence: int = Field(ge=3, le=20)
    wisdom: int = Field(ge=3, le=20)
    charisma: int = Field(ge=3, le=20)


class CharacterSheetV1(BaseModel):
    """Only player-authored base facts accepted by the first sheet slice."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    ancestry: str = Field(min_length=1, max_length=120)
    class_name: str = Field(min_length=1, max_length=120)
    level: int = Field(ge=1, le=20)
    ability_scores: AbilityScores

    @field_validator("ancestry", "class_name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("must not be blank")
        return normalized


def _visible_corpus_entity(
    session: Session,
    campaign_id: str,
    player_character_id: str,
    *,
    entity_type: Literal["race", "class"],
    name: str,
) -> Entity:
    matches = session.exec(
        visible_entities_query(campaign_id, player_character_id).where(
            Entity.entity_type == entity_type,
        )
    ).all()
    for entity, grant in matches:
        if entity.name.casefold() != name.casefold():
            continue
        if entity.is_global or (grant is not None and grant.grant_scope == "full"):
            return entity
    label = "ancestry" if entity_type == "race" else "class"
    raise SheetValidationError(f"{label} is not available in this campaign corpus")


def _class_hit_die(class_entity: Entity) -> int:
    detail = class_entity.detail or {}
    raw = detail.get("hit_die")
    if not isinstance(raw, str):
        raise SheetValidationError("class corpus entry has no supported hit die")
    match = _HIT_DIE.search(raw.strip())
    if match is None:
        raise SheetValidationError("class corpus entry has no supported hit die")
    return int(match.group(1))


def _class_saves(class_entity: Entity) -> set[Ability]:
    detail = class_entity.detail or {}
    raw = detail.get("saves")
    if not isinstance(raw, str) or not raw.strip():
        raise SheetValidationError("class corpus entry has no supported saving throws")

    normalized = raw.lower()
    saves: set[Ability] = set()
    for ability in ABILITIES:
        short = ability[:3]
        if re.search(rf"\b(?:{ability}|{short})\b", normalized):
            saves.add(ability)
    if not saves:
        raise SheetValidationError("class corpus entry has no supported saving throws")
    return saves


def derive_sheet(
    session: Session,
    campaign_id: str,
    player_character_id: str,
    sheet: CharacterSheetV1,
) -> dict:
    """Validate corpus selections and compute every exposed calculated value."""
    ancestry = _visible_corpus_entity(
        session,
        campaign_id,
        player_character_id,
        entity_type="race",
        name=sheet.ancestry,
    )
    character_class = _visible_corpus_entity(
        session,
        campaign_id,
        player_character_id,
        entity_type="class",
        name=sheet.class_name,
    )
    hit_die = _class_hit_die(character_class)
    proficient_saves = _class_saves(character_class)
    scores = sheet.ability_scores.model_dump()
    modifiers = {ability: (score - 10) // 2 for ability, score in scores.items()}
    proficiency_bonus = 2 + (sheet.level - 1) // 4
    saving_throws = {
        ability: modifiers[ability]
        + (proficiency_bonus if ability in proficient_saves else 0)
        for ability in ABILITIES
    }
    constitution = modifiers["constitution"]
    first_level_hp = max(1, hit_die + constitution)
    later_level_hp = max(1, hit_die // 2 + 1 + constitution)

    return {
        "ability_modifiers": modifiers,
        "proficiency_bonus": proficiency_bonus,
        "saving_throw_bonuses": saving_throws,
        "unarmored_armor_class": 10 + modifiers["dexterity"],
        "max_hit_points": first_level_hp + (sheet.level - 1) * later_level_hp,
        "hit_die": f"d{hit_die}",
        "corpus": {
            "ancestry_entity_id": ancestry.id,
            "class_entity_id": character_class.id,
        },
    }
