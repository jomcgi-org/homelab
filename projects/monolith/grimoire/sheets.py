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


def _usable_corpus_entities(
    session: Session,
    campaign_id: str,
    player_character_id: str,
    entity_type: Literal["race", "class"],
) -> list[Entity]:
    """Entities a sheet for this character may name, in a stable order.

    Global entries and ones granted with full scope. Partial and name-only
    grants do not count: the character does not know the mechanics.
    """
    rows = session.exec(
        visible_entities_query(campaign_id, player_character_id)
        .where(Entity.entity_type == entity_type)
        .order_by(Entity.name, Entity.id)
    ).all()
    return [
        entity
        for entity, grant in rows
        if entity.is_global or (grant is not None and grant.grant_scope == "full")
    ]


def _visible_corpus_entity(
    session: Session,
    campaign_id: str,
    player_character_id: str,
    *,
    entity_type: Literal["race", "class"],
    name: str,
) -> Entity:
    for entity in _usable_corpus_entities(
        session, campaign_id, player_character_id, entity_type
    ):
        if entity.name.casefold() == name.casefold():
            return entity
    label = "ancestry" if entity_type == "race" else "class"
    raise SheetValidationError(f"{label} is not available in this campaign corpus")


def _detail_text(entity: Entity, field: str) -> str | None:
    value = (entity.detail or {}).get(field)
    return value.strip() if isinstance(value, str) and value.strip() else None


def sheet_options(session: Session, campaign_id: str, player_character_id: str) -> dict:
    """The ancestries and classes ``derive_sheet`` would accept for a character.

    Each option carries the corpus facts the sheet form shows beside it. A
    name the sheet contract cannot carry, or one a same-named earlier entry
    already claims, is left out: ``derive_sheet`` could never select it.
    """

    def usable(entity_type: Literal["race", "class"]) -> list[Entity]:
        seen: set[str] = set()
        found = []
        for entity in _usable_corpus_entities(
            session, campaign_id, player_character_id, entity_type
        ):
            key = entity.name.casefold()
            fits = entity.name == " ".join(entity.name.split()) and (
                1 <= len(entity.name) <= 120
            )
            if fits and key not in seen:
                seen.add(key)
                found.append(entity)
        return found

    return {
        "ancestries": [
            {
                "id": entity.id,
                "name": entity.name,
                "size": _detail_text(entity, "size"),
                "speed": _detail_text(entity, "speed"),
            }
            for entity in usable("race")
        ],
        "classes": [
            {
                "id": entity.id,
                "name": entity.name,
                "hit_die": _detail_text(entity, "hit_die"),
                "primary_ability": _detail_text(entity, "primary_ability"),
                "saves": _detail_text(entity, "saves"),
            }
            for entity in usable("class")
        ],
    }


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
        "attack_bonuses": {
            "melee": modifiers["strength"] + proficiency_bonus,
            "ranged": modifiers["dexterity"] + proficiency_bonus,
        },
        "unarmored_armor_class": 10 + modifiers["dexterity"],
        "max_hit_points": first_level_hp + (sheet.level - 1) * later_level_hp,
        "hit_die": f"d{hit_die}",
        "corpus": {
            "ancestry_entity_id": ancestry.id,
            "class_entity_id": character_class.id,
        },
    }
