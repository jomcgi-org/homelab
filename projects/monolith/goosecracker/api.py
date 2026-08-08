"""goosecracker domain public API: the only surface other domains may import.

Other domains must import from
``goosecracker.api``, never from ``goosecracker`` internals such as
``goosecracker.repo_catalog`` (enforced by ``import_boundaries_test``).
"""

from __future__ import annotations

from goosecracker.recipe_catalog import CATALOG, enabled_enum  # re-exported
from goosecracker.repo_catalog import REPO_CATALOG, describe_repos  # re-exported

__all__ = [
    "CATALOG",
    "REPO_CATALOG",
    "describe_repos",
    "enabled_enum",
]
