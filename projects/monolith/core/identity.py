"""The verified caller identity a private-tier request carries, if any."""

from __future__ import annotations

from starlette.requests import Request


def verified_email(request: Request) -> str | None:
    """The email Envoy projected into X-Auth-Email from a verified Access JWT.

    Use this for attribution, never Cf-Access-Authenticated-User-Email: nothing
    in the cluster validates or strips that header, so any caller that reaches
    the backend can set it to any address (#6036, the class of gap #4628 was).
    X-Auth-Email is stripped by the gateway-wide ClientTrafficPolicy before the
    auth filter runs, so only Envoy's value can arrive.

    Envoy APPENDS its projected claim, so more than one value means a forged
    one arrived first. That, or no value at all, is None: the caller has no
    verified identity and the reader falls back to its anonymous actor.
    """
    projected = request.headers.getlist("x-auth-email")
    if len(projected) != 1:
        return None
    return projected[0].strip() or None
