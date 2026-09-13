"""Trusted identity input for Grimoire's private campaign API."""

from fastapi import HTTPException, Request


def get_authenticated_email(request: Request) -> str:
    """Return the verified email projected by the private Envoy gateway.

    The gateway strips inbound ``X-Auth-Email`` before validating the OIDC or
    bearer JWT and appending its signed email claim. Rejecting absent,
    duplicated, and empty values keeps this dependency fail-closed. Campaign
    authorization remains separate and always reads current database state.
    """
    values = request.headers.getlist("x-auth-email")
    if len(values) != 1:
        raise HTTPException(
            status_code=403,
            detail="missing or ambiguous X-Auth-Email header",
        )
    email = values[0].strip().lower()
    if not email:
        raise HTTPException(status_code=403, detail="missing authenticated email")
    return email
