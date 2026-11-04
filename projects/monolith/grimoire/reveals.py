"""Visible items in a single-entity or grouped knowledge reveal."""

from sqlmodel import Session

from grimoire.models import ENTITY_DETAIL_MODELS, Entity
from grimoire.visibility import project_entity, visible_entities_query


def current_reveal_body(session: Session, campaign_id: str, viewer, body: dict) -> dict:
    """Rebuild reveal snapshots from current grants and sources, never cached text."""
    if viewer == "dm":
        return body
    if body.get("retracted"):
        return {
            key: value
            for key, value in body.items()
            if key
            in (
                "entity_id",
                "name",
                "entity_type",
                "retracted",
                "silent",
                "grant_scope",
            )
        }
    items = []
    for item in reveal_items(body):
        if item.get("retracted"):
            # Retraction notifications have identity only, never old details.
            items.append(
                {
                    key: value
                    for key, value in item.items()
                    if key
                    in (
                        "entity_id",
                        "name",
                        "entity_type",
                        "retracted",
                        "silent",
                        "grant_scope",
                    )
                }
            )
            continue
        rows = session.exec(
            visible_entities_query(campaign_id, viewer)
            .execution_options(populate_existing=True)
            .where(Entity.id == item["entity_id"])
        ).all()
        if not rows:
            continue
        entity, grant = rows[0]
        model = ENTITY_DETAIL_MODELS.get(entity.entity_type)
        detail = (
            session.get(model, entity.id, populate_existing=True) if model else None
        )
        projection = project_entity(entity, detail, grant, viewer)
        if projection is None:
            continue
        scope = (
            "name_only"
            if projection.get("recognition_only")
            else "partial"
            if "revealed_details" in projection
            else "full"
        )
        current = {
            "entity_id": entity.id,
            "name": entity.name,
            "entity_type": entity.entity_type,
            "grant_scope": scope,
        }
        if scope != "name_only":
            current["entity"] = projection
        items.append(current)
    return {"reveals": items} if "reveals" in body else (items[0] if items else {})


def reveal_items(body: dict) -> list[dict]:
    items = body.get("reveals", [body])
    if not isinstance(items, list):
        return []
    removed = body.get("retracted_entity_ids", [])
    return [
        item
        for item in items
        if isinstance(item, dict)
        and isinstance(item.get("entity_id"), str)
        and item["entity_id"] not in removed
    ]
