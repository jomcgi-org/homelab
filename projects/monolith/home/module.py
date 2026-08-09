"""FastMonolith module export for the home domain (see framework/core.py).

Lives in its own file (not __init__.py) so importing the domain package does
not pull the framework or FastAPI: standalone binaries that reuse domain code
(e.g. trips_backfill, the knowledge tools) glob only their own sources.
"""

import home as _domain

from core.platform_probe import probe_health

from framework import Module as _Module

# 2.5x the writer's default 300s cadence, so one slow or missed cycle never
# flaps the endpoint but a dead writer still surfaces. Same reasoning as the
# ember synthetic components.
_CD_STALENESS_S = 750.0


async def _startup(app):
    """Prime the observability snapshots (topology + stats) once at startup so
    the first request has data; the scheduled rollup jobs refresh thereafter.
    Runs on every replica (best-effort)."""
    from home.observability.rollup import prime_snapshots

    await prime_snapshots()


MODULE = _Module(
    name="home",
    register=_domain.register,
    register_public=_domain.register_public,
    startup=_startup,
    # cd platform health is REPORTED here, not computed here. The cluster
    # domain computes it privately and writes the platform_probe latch; this
    # module is in both the private and public registries, so registering the
    # reader here is what puts the component on the tier UptimeRobot polls.
    # The public tier needs no new privilege to serve it, which is the point.
    register_health={"cd": probe_health("cd", _CD_STALENESS_S)},
)
