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
from auth.settings import AuthSettings
from auth.verifier import AuthentikStandingVerifier


def platform_enforcement_enabled():
    import os

    return os.getenv("PLATFORM_AUTH_ENFORCEMENT_ENABLED", "") == "true"


def require_application_permission(session, principal, permission):
    """Private applications opt in without importing persistence on public startup."""
    from auth.platform.service import require_permission

    return require_permission(session, principal, permission)


def bind_application_user(session, principal, application, application_user_id):
    from auth.platform.service import bind_application_user as bind

    return bind(session, principal, application, application_user_id)


def find_application_user_by_username(session, application, username):
    if not platform_enforcement_enabled():
        return None
    from auth.platform.service import find_application_user_by_username as find

    return find(session, application, username)


__all__ = [
    "AuthError",
    "AuthErrorReason",
    "AuthSettings",
    "AuthentikStandingVerifier",
    "Authority",
    "Principal",
    "PrincipalKind",
    "PrincipalMiddleware",
    "anonymous_principal",
    "auth_error_handler",
    "bind_application_user",
    "current_principal",
    "get_default_resolver",
    "find_application_user_by_username",
    "get_principal",
    "platform_enforcement_enabled",
    "require_application_permission",
]
