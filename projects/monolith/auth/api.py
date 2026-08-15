"""Public authentication API for monolith domains."""

from auth.dependencies import (
    auth_error_handler,
    current_principal,
    get_default_resolver,
    get_principal,
)
from auth.errors import AuthError, AuthErrorReason
from auth.middleware import PrincipalMiddleware
from auth.principal import Authority, Principal, PrincipalKind, anonymous_principal

__all__ = [
    "AuthError",
    "AuthErrorReason",
    "Authority",
    "Principal",
    "PrincipalKind",
    "anonymous_principal",
    "current_principal",
    "get_principal",
    "get_default_resolver",
    "PrincipalMiddleware",
    "auth_error_handler",
]
