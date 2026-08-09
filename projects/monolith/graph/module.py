from framework import Module as _Module


def register(app) -> None:
    from graph.router import router

    app.include_router(router)


async def _leader_start(app):
    from graph import runtime

    runtime.launch()
    return []


MODULE = _Module(name="graph", register=register, leader_start=_leader_start)
