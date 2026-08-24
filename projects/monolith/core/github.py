"""Shared GitHub repository configuration.

The repository moved from ``jomcgi/homelab`` to ``jomcgi-org/homelab`` on
2026-08-22. GitHub returns a 301 redirect for the old path, and httpx does not
follow redirects by default.
"""

import os

GITHUB_API = "https://api.github.com"
GITHUB_REPO = os.environ.get("GITHUB_REPO", "jomcgi-org/homelab")
