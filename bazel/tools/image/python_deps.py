"Shim binary for packaging Python runtime + pip deps into tar layers."

import httpx  # noqa: F401 — needed in tools image for homelab CLI
import typer  # noqa: F401 — needed in tools image for homelab CLI
import yaml  # noqa: F401 — needed by ci lint for document bundles
