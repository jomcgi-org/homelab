"""Private monolith entrypoint: the confined monolith (all domains).

Composed by the FastMonolith framework (ADR services/010): one ``build_app``
call over the full module registry. Everything this file used to hand-author
(lifespan, leader election, MCP mount, OTel, static frontend, health) lives in
``framework/core.py``; the leader-elected singletons live with their owning
domains (``chat/leader.py``, ``ships/leader.py``).
"""

from __future__ import annotations

from app.modules_private import ALL_MODULES
from framework import PRIVATE_PROFILE, build_app

app = build_app(PRIVATE_PROFILE, ALL_MODULES)


if __name__ == "__main__":
    import uvicorn
    from factory.module import RolloutHandoffServer
    from factory.execution.store import rollout_handoff_enabled

    if rollout_handoff_enabled():
        RolloutHandoffServer(
            # Lifespan handoff follows HTTP drain. Bound that wait at 5s so
            # the 15s executor budget fits inside the chart's 45s grace, including the 10s routing delay (#6670).
            uvicorn.Config(
                app,
                host="0.0.0.0",
                port=8000,
                log_level="warning",
                timeout_graceful_shutdown=5,
            )
        ).run()
    else:
        uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
