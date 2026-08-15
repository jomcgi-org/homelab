"""Authentication failures classified by their owner."""

from __future__ import annotations

import enum

from starlette.exceptions import HTTPException


class AuthErrorReason(enum.StrEnum):
    """Stable failure reasons for callers, logs, and tests."""

    BAD_SIGNATURE = "bad_signature"
    WRONG_ISSUER = "wrong_issuer"
    WRONG_AUDIENCE = "wrong_audience"
    EXPIRED = "expired"
    NOT_YET_VALID = "not_yet_valid"
    MALFORMED = "malformed"
    UNKNOWN_KID = "unknown_kid"
    UNRECOGNIZED = "unrecognized"
    JWKS_UNREACHABLE = "jwks_unreachable"
    JWKS_MALFORMED = "jwks_malformed"
    UNCONFIGURED = "unconfigured"

    @property
    def is_infrastructure_fault(self) -> bool:
        return self in {
            AuthErrorReason.JWKS_UNREACHABLE,
            AuthErrorReason.JWKS_MALFORMED,
            AuthErrorReason.UNCONFIGURED,
        }


class AuthError(HTTPException):
    """An explicit authentication failure with safe HTTP semantics."""

    def __init__(self, reason: AuthErrorReason):
        self.reason = reason
        status_code = 503 if reason.is_infrastructure_fault else 401
        headers = None if status_code == 503 else {"WWW-Authenticate": "Bearer"}
        super().__init__(
            status_code=status_code,
            detail=f"authentication failed: {reason.value}",
            headers=headers,
        )
