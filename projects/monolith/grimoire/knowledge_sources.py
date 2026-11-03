"""Transaction-local source reconciliation. Callers own locking and commit."""

from sqlalchemy import func
from sqlmodel import Session, select

from grimoire.models import (
    CharacterSheetVersion,
    Entity,
    KnowledgeContribution,
    KnowledgeGrant,
    PlayerCharacter,
    Relationship,
)
from grimoire.visibility import common_knowledge_predicate

_RANK = {"name_only": 0, "partial": 1, "full": 2}
MAX_CREATION_LINKS = 100


def lock_player(session: Session, campaign_id: str, pc_id: str) -> None:
    session.exec(
        select(PlayerCharacter)
        .where(
            PlayerCharacter.id == pc_id,
            PlayerCharacter.campaign_id == campaign_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()


def contributions(
    session: Session, grant: KnowledgeGrant
) -> list[KnowledgeContribution]:
    return list(
        session.exec(
            select(KnowledgeContribution).where(
                KnowledgeContribution.grant_id == grant.id
            )
        ).all()
    )


def preserve_manual(session: Session, grant: KnowledgeGrant) -> None:
    """Compatibility for existing rows, also backfilled by the migration."""
    if not contributions(session, grant):
        session.add(
            KnowledgeContribution(
                grant_id=grant.id,
                source_key="manual",
                grant_scope=grant.grant_scope,
                revealed_details=grant.revealed_details,
                granted_in_session=grant.granted_in_session,
            )
        )
        session.flush()


def recompute(session: Session, grant: KnowledgeGrant) -> bool:
    """Return whether any source survives; callers handle events and deletion."""
    sources = contributions(session, grant)
    if not sources:
        return False
    grant.grant_scope = max(
        sources, key=lambda source: _RANK[source.grant_scope]
    ).grant_scope
    details = {}
    for source in sorted(sources, key=lambda source: source.source_key):
        details.update(source.revealed_details or {})
    grant.revealed_details = details or None
    manual = next((source for source in sources if source.source_key == "manual"), None)
    grant.granted_in_session = manual.granted_in_session if manual else None
    return True


def set_manual(
    session: Session,
    grant: KnowledgeGrant,
    *,
    scope: str,
    details: dict | None,
    game_session: str | None,
) -> None:
    source = next(
        (
            source
            for source in contributions(session, grant)
            if source.source_key == "manual"
        ),
        None,
    )
    if source is None:
        source = KnowledgeContribution(
            grant_id=grant.id, source_key="manual", grant_scope=scope
        )
        session.add(source)
    source.grant_scope = scope
    source.revealed_details = details
    source.granted_in_session = game_session
    session.flush()
    recompute(session, grant)


def remove_manual(session: Session, grant: KnowledgeGrant) -> bool:
    preserve_manual(session, grant)
    for source in contributions(session, grant):
        if source.source_key == "manual":
            session.delete(source)
    session.flush()
    return recompute(session, grant)


def creation_entities(session: Session, version: CharacterSheetVersion) -> set[str]:
    """One-hop typed links only. Never follow arbitrary adventure links."""
    selected = {}
    for field, entity_type in (("ancestry", "race"), ("class_name", "class")):
        matches = session.exec(
            select(Entity)
            .where(
                common_knowledge_predicate(),
                Entity.entity_type == entity_type,
                func.lower(Entity.name) == version.sheet[field].lower(),
            )
            .order_by(Entity.id)
            .limit(2)
        ).all()
        if len(matches) != 1:
            raise ValueError(f"{field} must resolve to one common-knowledge entity")
        selected[field] = matches[0].id
    roots = set(selected.values())
    links = session.exec(
        select(Entity, Relationship)
        .join(Relationship, Relationship.from_entity_id == Entity.id)
        .where(
            common_knowledge_predicate(),
            Relationship.to_entity_id == selected["class_name"],
            Relationship.rel_type == "FEATURE_OF",
            Entity.entity_type == "class_feature",
        )
        .order_by(Entity.id)
        .limit(MAX_CREATION_LINKS + 1)
    ).all()
    if len(links) > MAX_CREATION_LINKS:
        raise ValueError("too many creation knowledge links")
    related = set()
    for entity, _ in links:
        level = (entity.detail or {}).get("level")
        if type(level) is int and 1 <= level <= version.sheet["level"]:
            related.add(entity.id)
    # SUBRACE_OF points from the selected ancestry to its parent race.
    parents = session.exec(
        select(Entity)
        .join(Relationship, Relationship.to_entity_id == Entity.id)
        .where(
            common_knowledge_predicate(),
            Relationship.from_entity_id == selected["ancestry"],
            Relationship.rel_type == "SUBRACE_OF",
            Entity.entity_type == "race",
        )
        .order_by(Entity.id)
        .limit(MAX_CREATION_LINKS + 1)
    ).all()
    if len(parents) > MAX_CREATION_LINKS:
        raise ValueError("too many ancestry knowledge links")
    return roots | related | {entity.id for entity in parents}


def reconcile_sheet(
    session: Session, version: CharacterSheetVersion
) -> list[tuple[KnowledgeGrant, bool]]:
    """Replace the PC's effective approved-sheet source, preserving manual data."""
    desired = creation_entities(session, version)
    grants = list(
        session.exec(
            select(KnowledgeGrant)
            .where(
                KnowledgeGrant.campaign_id == version.campaign_id,
                KnowledgeGrant.player_character_id == version.player_character_id,
            )
            .with_for_update()
        ).all()
    )
    existing = {grant.entity_id: grant for grant in grants}
    before = {
        grant.entity_id: (grant.grant_scope, grant.revealed_details) for grant in grants
    }
    added = [
        KnowledgeGrant(
            campaign_id=version.campaign_id,
            player_character_id=version.player_character_id,
            entity_id=entity_id,
            grant_scope="full",
        )
        for entity_id in desired - existing.keys()
    ]
    for grant in grants:
        preserve_manual(session, grant)
    session.add_all(added)
    session.flush()
    grants.extend(added)
    additions = []
    for grant in grants:
        for source in contributions(session, grant):
            if source.source_sheet_version_id is not None:
                session.delete(source)
        if grant.entity_id in desired:
            additions.append(
                KnowledgeContribution(
                    grant_id=grant.id,
                    source_key=f"sheet:{version.id}",
                    source_sheet_version_id=version.id,
                    grant_scope="full",
                )
            )
    session.flush()
    session.add_all(additions)
    session.flush()
    changes = []
    for grant in grants:
        survives = recompute(session, grant)
        if not survives or before.get(grant.entity_id) != (
            grant.grant_scope,
            grant.revealed_details,
        ):
            changes.append((grant, survives))
    return changes
