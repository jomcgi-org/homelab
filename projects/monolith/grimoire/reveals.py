"""Visible items in a single-entity or grouped knowledge reveal."""


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
