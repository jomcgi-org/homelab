"""Existing admission and decision-read contracts for monolith consumers.

The factory's HTTP and MCP controls remain its private interaction surface.
These functions preserve the routine-job and knowledge intervention contracts.
"""

from factory.execution import SUPPORTED_MODELS as SUPPORTED_MODELS
from factory.execution.api import (
    DRAINER_NODE_KEY as DRAINER_NODE_KEY,
    KG_NODE_KEY as KG_NODE_KEY,
    accept_review_recomputation as accept_review_recomputation,
    confirm_reconciled_guest_cessation as confirm_reconciled_guest_cessation,
    confirm_reconciled_unbound_attempt as confirm_reconciled_unbound_attempt,
    lock_capacity_pool as lock_capacity_pool,
    lock_cessation_session as lock_cessation_session,
    read_review_cessation_observation as read_review_cessation_observation,
    observe_guest_for_recomputation as observe_guest_for_recomputation,
)


def operator_access_error(principal) -> str | None:
    """Share the private operator gate without expanding admission-only imports."""
    from factory.access import OPERATOR_REQUIRED, is_operator

    return None if is_operator(principal) else OPERATOR_REQUIRED
