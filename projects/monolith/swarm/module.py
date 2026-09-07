from framework import Module as _Module

from knowledge.api import kg_health
from swarm.health import drainer_health


def register(app) -> None:
    from swarm.factory_router import router as factory_router
    from swarm.drain_console_router import router as drain_console_router
    from swarm.drainer_router import router as drainer_router
    from swarm.router import router

    app.include_router(router)
    app.include_router(drainer_router)
    app.include_router(drain_console_router)
    app.include_router(factory_router)


async def _leader_start(app):
    from swarm import runtime
    from swarm.factory_conductor import start_loop
    from swarm import node_workflows  # noqa: F401 - register before DBOS launch

    runtime.launch()
    app.state.leader_singletons_dbos_launched = runtime.is_launched()
    return start_loop() if runtime.is_launched() else []


async def _leader_stop(app):
    from swarm import runtime

    try:
        runtime.shutdown()
    finally:
        app.state.leader_singletons_dbos_launched = False


MODULE = _Module(
    name="swarm",
    leader_priority=0,
    register=register,
    leader_start=_leader_start,
    leader_stop=_leader_stop,
    register_health_advisory={"drainer": drainer_health, "kg": kg_health},
)
