"""Environment-backed authentication settings."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("monolith.auth")


@dataclass(frozen=True, slots=True)
class AuthSettings:
    authentik_jwks_url: str
    authentik_issuer: str
    authentik_audience: str
    jwks_cache_ttl_s: float

    @classmethod
    def from_env(cls) -> AuthSettings:
        def parse_ttl(value: str) -> float:
            try:
                return float(value)
            except ValueError:
                logger.warning(
                    "invalid AUTH_JWKS_CACHE_TTL_S: %r, falling back to 300s", value
                )
                return 300.0

        return cls(
            authentik_jwks_url=os.getenv("AUTH_AUTHENTIK_JWKS_URL", ""),
            authentik_issuer=os.getenv("AUTH_AUTHENTIK_ISSUER", ""),
            authentik_audience=os.getenv("AUTH_AUTHENTIK_AUDIENCE", ""),
            jwks_cache_ttl_s=parse_ttl(os.getenv("AUTH_JWKS_CACHE_TTL_S", "300")),
        )

    @property
    def identity_is_configured(self) -> bool:
        return all(
            (
                self.authentik_jwks_url,
                self.authentik_issuer,
                self.authentik_audience,
            )
        )
