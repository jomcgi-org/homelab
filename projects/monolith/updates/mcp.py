"""Scoped MCP submission tool for the private product-update journal."""

from __future__ import annotations

import asyncio

from auth.api import current_principal
from core.mcp_app import mcp
from updates.schemas import ProductUpdateSubmission
from updates.store import UpdateAlreadyPublished, compare_url, publish_update

SUBMIT_SCOPE = "updates:submit"


async def submit_product_update(update: ProductUpdateSubmission) -> dict:
    """Publish one structured daily update to private.jomcgi.dev/updates.

    The caller must hold the ``updates:submit`` scope. The update is immediately
    visible on the private journal after this tool accepts it. Dates and source
    heads are immutable, while an exact replay is an idempotent success.
    """
    principal = current_principal()
    if not principal.has_scope(SUBMIT_SCOPE):
        return {
            "accepted": False,
            "error": f"caller requires the {SUBMIT_SCOPE!r} scope",
        }

    try:
        row, created = await asyncio.to_thread(publish_update, update, principal)
    except UpdateAlreadyPublished as exc:
        return {"accepted": False, "error": str(exc)}

    anchor = row.published_on.isoformat()
    return {
        "accepted": True,
        "created": created,
        "published_on": anchor,
        "url": f"https://private.jomcgi.dev/updates#update-{anchor}",
        "source_compare_url": compare_url(row.source_base_sha, row.source_head_sha),
    }


def register_mcp_tools() -> None:
    """Register the sole write capability for the updates domain."""
    mcp.tool(name="submit_product_update")(submit_product_update)
