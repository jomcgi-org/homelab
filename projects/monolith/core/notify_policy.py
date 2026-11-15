"""The factory's standing workload grant is confined to default-channel notify."""

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


def factory_notify_permitted(principal) -> bool:
    from auth.principal import Authority, PrincipalKind

    issuer = os.environ.get("AUTH_FACTORY_NOTIFY_ISSUER", "")
    return bool(
        issuer
        and principal.issuer == issuer
        and principal.username == "factory-notify-sa"
        and principal.groups == ("factory-notify",)
        and principal.kind is PrincipalKind.WORKLOAD
        and principal.authority is Authority.STANDING
        and not principal.actor
        and not principal.delegation_claim_present
    )


def check_notify_arguments(principal, message, channel, dedupe_key) -> None:
    if not is_factory_notify(principal):
        return
    from fastmcp.exceptions import AuthorizationError

    if not factory_notify_permitted(principal) or channel is not None:
        raise AuthorizationError("factory notify requires its default channel")
    if not message or len(message.encode("utf-8")) > 1000 or not dedupe_key:
        raise ValueError("factory notify requires bounded text and a dedupe key")
