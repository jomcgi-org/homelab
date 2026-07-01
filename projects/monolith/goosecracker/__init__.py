"""goosecracker: run goose turns via the fc-invoke daemon (goose cutover PR C).

Trigger-agnostic core: ``submit`` writes a run ledger row and fires an fc-invoke
run + result delivery off detached; ``status`` / ``list_runs`` / ``get_run`` read
the ledger. Thin adapters (chat's /artifact command, agent's MCP tools) call
``submit`` and poll the ledger; they own no execution logic.
"""

from __future__ import annotations

from goosecracker.dispatch import resume, status, submit
from goosecracker.threads import get_run, list_runs

__all__ = ["get_run", "list_runs", "resume", "status", "submit"]
