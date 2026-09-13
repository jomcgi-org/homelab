"""Public, read-only FastAPI entrypoint.

Composed by the FastMonolith framework (ADR services/010) from the public
module registry: only each domain's ``register_public`` surface is mounted.
The public profile deliberately omits everything the public tier must never
run: the Discord bot, leader singletons, the ``/mcp`` mount, and the SvelteKit
static mount. OpenTelemetry setup is gated on its endpoint env. There is no
lifespan, so importing this module remains cheap when that endpoint is absent.

The deep ``/api/health`` route (SELECT 1 via the public_reader role on the
read replica) comes from the profile's ``deep_health`` flag; see
framework/core.py.
"""

from __future__ import annotations

import os

from app.modules_public import PUBLIC_MODULES
from core.db import require_database_identity
from framework import PUBLIC_PROFILE, build_app

_expected_database_service = os.environ.get("PUBLIC_READER_DATABASE_SERVICE")
_expected_database_user = os.environ.get("PUBLIC_READER_DATABASE_USER")
if bool(_expected_database_service) != bool(_expected_database_user):
    raise RuntimeError(
        "PUBLIC_READER_DATABASE_SERVICE and PUBLIC_READER_DATABASE_USER "
        "must be configured together"
    )
if _expected_database_service and _expected_database_user:
    require_database_identity(_expected_database_service, _expected_database_user)

app = build_app(PUBLIC_PROFILE, PUBLIC_MODULES)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
