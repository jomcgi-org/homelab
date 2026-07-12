"""FastMonolith: the monolith's thin composition framework (ADR services/010).

One ``build_app(profile, modules)`` composes any set of domain ``Module``s into
a FastAPI app for a given ``Profile`` (tier + capabilities). The three shapes:

- the confined monolith: ``build_app(PRIVATE_PROFILE, ALL_MODULES)``
- the public read-only tier: ``build_app(PUBLIC_PROFILE, PUBLIC_MODULES)``
- any single domain: ``build_app(domain_profile(name), [MODULE])``

The framework is deliberately tiny: no base classes, no DI container, no
auto-discovery. A module is plain data plus callables.
"""

from framework.core import (
    PRIVATE_PROFILE,
    PUBLIC_PROFILE,
    Module,
    Profile,
    Tier,
    build_app,
    build_private_lifespan,
    domain_profile,
    log_task_exception,
    start_leader_singletons,
    stop_leader_singletons,
)

__all__ = [
    "PRIVATE_PROFILE",
    "PUBLIC_PROFILE",
    "Module",
    "Profile",
    "Tier",
    "build_app",
    "build_private_lifespan",
    "domain_profile",
    "log_task_exception",
    "start_leader_singletons",
    "stop_leader_singletons",
]
