from framework import Module as _Module


def register(app) -> None:
    from swarm.drainer_router import router as drainer_router
    from swarm.router import router

    app.include_router(router)
    app.include_router(drainer_router)


async def _leader_start(app):
    from swarm import runtime

    runtime.launch()
    return []


MODULE = _Module(name="swarm", register=register, leader_start=_leader_start)
