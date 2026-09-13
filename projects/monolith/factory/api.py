"""Existing admission and decision-read contracts for monolith consumers.

The factory's HTTP and MCP controls remain its private interaction surface.
These functions preserve the routine-job and knowledge intervention contracts.
"""

from factory.execution.api import (
    DRAINER_NODE_KEY as DRAINER_NODE_KEY,
    KG_NODE_KEY as KG_NODE_KEY,
    confirm_reconciled_guest_cessation as confirm_reconciled_guest_cessation,
    lock_capacity_pool as lock_capacity_pool,
    lock_cessation_session as lock_cessation_session,
)
from factory.orchestration.api import get_decision_reference as get_decision_reference
