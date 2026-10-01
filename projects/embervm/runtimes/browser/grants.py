"""Task-scoped browser execution capabilities. This module issues no grants."""

import ipaddress
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

ACTIONS = frozenset(("navigate", "click", "fill", "submit", "upload", "script"))


def origin(url: str) -> str:
    """Return an exact scheme/host/effective-port origin, or refuse the URL.

    No DNS or network access is performed. The future egress adapter must also
    reject DNS resolutions and redirects to metadata or other ungranted origins.
    """
    if not isinstance(url, str) or not url or re.search(r"[\s\\\x00-\x1f\x7f]", url):
        raise ValueError("URL must be a nonempty HTTP(S) URL without whitespace")
    try:
        parsed = urlsplit(url)
        host, port = parsed.hostname, parsed.port
        if (
            parsed.scheme not in ("http", "https")
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or "%" in host
            or parsed.netloc.endswith(":")
        ):
            raise ValueError("invalid URL origin or userinfo")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # Refuse alternate numeric spellings interpreted as IPv4 by browsers.
            if not re.fullmatch(r"[a-z0-9.-]+", host) or host.endswith("."):
                raise ValueError("ambiguous host") from None
            if any(not label for label in host.split(".")):
                raise ValueError("empty host label")
            last = host.split(".")[-1]
            if last.isdigit() or re.fullmatch(r"0x[0-9a-f]+", last):
                raise ValueError("noncanonical numeric host") from None
        else:
            mapped = getattr(address, "ipv4_mapped", None)
            if address.is_link_local or (mapped and mapped.is_link_local):
                raise ValueError("link-local metadata destinations are forbidden")
            host = str(address)
            if address.version == 6:
                host = "[" + host + "]"
        port = port if port is not None else (443 if parsed.scheme == "https" else 80)
        if port <= 0:
            raise ValueError("port must be positive")
        return f"{parsed.scheme}://{host}:{port}"
    except (TypeError, ValueError) as error:
        raise ValueError("invalid browser URL: " + str(error)) from error


def _timestamp(value):
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("time must be timezone-aware")
    if not math.isfinite(value.timestamp()):
        raise ValueError("time must be finite")
    return value


@dataclass(frozen=True)
class ActionGrant:
    task_id: str
    principal: str
    allowed_origins: tuple[str, ...] = ()
    allowed_actions: frozenset[str] = frozenset()
    expires_at: datetime | None = None

    def __post_init__(self):
        if any(
            not isinstance(v, str) or not v.strip()
            for v in (self.task_id, self.principal)
        ):
            raise ValueError("task_id and principal are required")
        if not isinstance(self.allowed_origins, tuple):
            raise ValueError("origins must be a tuple")
        if len(set(self.allowed_origins)) != len(self.allowed_origins):
            raise ValueError("duplicate origins")
        for allowed in self.allowed_origins:
            if origin(allowed) != allowed:
                raise ValueError("allowed origins require exact scheme, host and port")
        if (
            not isinstance(self.allowed_actions, frozenset)
            or not self.allowed_actions <= ACTIONS
        ):
            raise ValueError("unknown actions or invalid action set")
        if self.expires_at is not None:
            _timestamp(self.expires_at)
        if (self.allowed_origins or self.allowed_actions) and self.expires_at is None:
            raise ValueError("nonempty grants require an expiry")


@dataclass(frozen=True)
class GrantVerdict:
    allowed: bool
    reason: str


def check(grant, owner, action, url, *, now=None) -> GrantVerdict:
    """Check every action at dispatch, including navigate and script execution."""
    try:
        # The owner is the supervisor's SessionOwner, imported here to avoid
        # allowing arbitrary duck-typed objects to stand in for an identity.
        from session import SessionOwner

        if not isinstance(grant, ActionGrant) or not isinstance(owner, SessionOwner):
            raise ValueError("missing grant or session owner")
        grant.__post_init__()
        owner.__post_init__()
        if (grant.task_id, grant.principal) != (owner.task_id, owner.principal):
            raise ValueError("task or principal mismatch")
        current = _timestamp(datetime.now(timezone.utc) if now is None else now)
        if grant.expires_at is None or current >= grant.expires_at:
            raise ValueError("grant absent or expired")
        if not isinstance(action, str) or action not in grant.allowed_actions:
            raise ValueError("action not granted")
        if origin(url) not in grant.allowed_origins:
            raise ValueError("origin not granted")
    except (ValueError, TypeError, AttributeError) as error:
        return GrantVerdict(False, str(error))
    return GrantVerdict(True, "task, principal, expiry, action and origin match")
