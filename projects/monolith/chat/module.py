"""FastMonolith module export for the chat domain (see framework/core.py).

Lives in its own file (not __init__.py) so importing the domain package does
not pull the framework or FastAPI: standalone binaries that reuse domain code
(e.g. trips_backfill, the knowledge tools) glob only their own sources.
"""

import chat as _domain

from framework import Module as _Module


async def _leader_start(app):
    """Start the chat leader singletons (lazy import keeps __init__ light)."""
    from chat.leader import leader_start

    return await leader_start(app)


async def _leader_stop(app):
    from chat.leader import leader_stop

    await leader_stop(app)


MODULE = _Module(
    name="chat",
    register=_domain.register,
    leader_start=_leader_start,
    leader_stop=_leader_stop,
    requires_secrets=frozenset({"DISCORD_BOT_TOKEN"}),
)
