"""Identify the retired factory workload so the monolith denies it access."""

from __future__ import annotations

import os


def is_factory_notify(principal) -> bool:
    # Recognize even a malformed/over-granted identity so it never falls back
    # to operator or public-tool permissions, including with the escape hatch.
    issuer = os.environ.get("AUTH_FACTORY_NOTIFY_ISSUER", "")
    return (
        bool(issuer and principal.issuer == issuer)
        or principal.username == "factory-notify-sa"
        or principal.has_group("factory-notify")
    )
