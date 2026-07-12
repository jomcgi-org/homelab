"""Single-domain entrypoint: compose one domain as its own binary/image.

``MONOLITH_DOMAIN`` selects the domain package (which must export ``MODULE``
from its ``module.py``; see app/modules_private.py DOMAIN_NAMES for the valid
set). The image macro in projects/monolith/BUILD bakes one image per domain
from this shared binary, so every domain image reuses the confined monolith's
dependency layers; only the baked env differs.

Per ADR services/010, artifact contents are not the security boundary: the
domain image carries the full backend closure, and what the running process
can actually do is decided by the secrets and DB role its deployment injects.
"""

from __future__ import annotations

import importlib
import os

from framework import build_app, domain_profile

_name = os.environ.get("MONOLITH_DOMAIN", "")
if not _name:
    raise RuntimeError(
        "MONOLITH_DOMAIN must name a domain package (e.g. 'hikes'); "
        "see app/modules_private.py DOMAIN_NAMES for the valid set"
    )

MODULE = importlib.import_module(_name + ".module").MODULE
app = build_app(domain_profile(_name), [MODULE])

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="warning")
