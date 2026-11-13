"""Manual, default-off definition job. No serving or startup integration."""

import json
import logging
import os

from grimoire.loom.client import LoomClient
from grimoire.loom.ontology import Partition, apply

logger = logging.getLogger(__name__)


async def grimoire_loom_ontology() -> None:
    """Apply explicit working-set definitions with a reserved-admin Loom subject.

    Dataset Read/Write does not authorize /admin/models, /admin/links or
    /admin/actions. The separate enablement owner must select and configure the
    administrative subject, serialize definition administrators, prepare tables
    and grants, and authorize manual execution. Never fall back to the
    dataset-scoped loom-grimoire-token. No administrative secret is provisioned.
    """
    if os.environ.get("GRIMOIRE_LOOM_ENABLED") != "true":
        logger.info("grimoire-loom-ontology disabled")
        return
    token = os.environ.get("GRIMOIRE_LOOM_ADMIN_TOKEN", "")
    base_url = os.environ.get("GRIMOIRE_LOOM_BASE_URL", "")
    if not token or not base_url:
        raise ValueError(
            "explicit Loom job-admin token and base URL required; runtime token is never used"
        )
    raw = json.loads(os.environ.get("GRIMOIRE_LOOM_PARTITIONS", "[]"))
    if not isinstance(raw, list) or not raw:
        raise ValueError("explicit campaign/PC partition configuration required")
    if any(not isinstance(entry, dict) for entry in raw):
        raise ValueError("each partition must be a campaign/PC configuration object")
    partitions = tuple(Partition(**entry) for entry in raw)
    if len({p.schema for p in partitions}) != len(partitions):
        raise ValueError("duplicate partitions")
    async with LoomClient(base_url, token) as client:
        writes = await apply(client, partitions)
    logger.info(
        "grimoire-loom-ontology applied %d partitions, eight types each, %d definition writes",
        len(partitions),
        writes,
    )
