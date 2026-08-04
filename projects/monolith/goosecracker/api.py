"""goosecracker domain public API: the only surface other domains may import.

Other domains (chat's /agent command, agent's MCP tools) must import from
``goosecracker.api``, never from ``goosecracker`` internals such as
``goosecracker.runner`` (enforced by ``import_boundaries_test``).
"""

from __future__ import annotations

from goosecracker.dispatch import status  # re-exported
from goosecracker.recipe_catalog import CATALOG, enabled_enum  # re-exported
from goosecracker.repo_catalog import REPO_CATALOG, describe_repos  # re-exported
from goosecracker.router_render import (  # re-exported
    render_plan_file,
    render_router,
    stage_title,
)
from goosecracker.threads import get_run, list_runs, serialize  # re-exported
from goosecracker.tiers import features_for_tier, tier_allows  # re-exported

__all__ = [
    "CATALOG",
    "REPO_CATALOG",
    "describe_repos",
    "enabled_enum",
    "features_for_tier",
    "get_run",
    "list_runs",
    "render_plan_file",
    "render_router",
    "serialize",
    "stage_title",
    "status",
    "tier_allows",
]
