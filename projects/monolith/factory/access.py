"""Authorization for factory controls and private MCP projections."""

from __future__ import annotations

import os

from fastapi import Depends, HTTPException, Request

from auth.api import Authority, Principal, PrincipalKind, get_principal

OPERATOR_GROUP = "operators"
OPERATOR_REQUIRED = "standing operator authority is required"


def is_operator(principal: Principal) -> bool:
    """Factory bearer access requires a standing human operator."""
    return (
        principal.authority == Authority.STANDING
        and principal.kind == PrincipalKind.HUMAN
        and principal.has_group(OPERATOR_GROUP)
    )


def operator(principal: Principal = Depends(get_principal)) -> Principal:
    if not is_operator(principal):
        raise HTTPException(403, OPERATOR_REQUIRED)
    return principal


def factory_decider(request: Request) -> str:
    """Who is allowed to answer an escalation from the private tier browser.

    The operator-gated /api/swarm/factory routes want a standing bearer, and
    the browser behind Cloudflare Access does not carry one: that is why the
    board next door is a view rather than a control. A decision IS a control,
    so it needs an identity, and the only verified one the browser has is the
    email claim Envoy projects into X-Auth-Email from the Access JWT.

    X-Auth-Email and NOT Cf-Access-Authenticated-User-Email. The projected
    header is the one the gateway-wide ClientTrafficPolicy strips on ingress
    before the auth filter runs, so the only value that can arrive is the one
    Envoy put there from a signature it verified. The Cf-Access-* header is
    neither validated by anything in the cluster nor stripped at the listener,
    so a caller that reaches the backend can set it to any address they like
    (the class of gap #4628 was). It is read here only for attribution when
    the verified header agrees with it, never for the authorization decision.

    Access is the gate, so the verified address is enough on its own.
    private.jomcgi.dev is zero trust locked to one identity, and a second list
    behind that would only restate what Access already decided, so
    FACTORY_OPERATOR_EMAILS is empty by default and any single verified
    identity decides. Set it to narrow that, granting someone the page without
    the buttons: non-empty it is an allowlist, and an address missing from it
    gets a 403 on the click.
    Agents holding a real bearer use POST /api/swarm/factory/decisions/{id}
    and never reach here.
    """
    allowed = {
        entry.strip().lower()
        for entry in os.environ.get("FACTORY_OPERATOR_EMAILS", "").split(",")
        if entry.strip()
    }
    # Defence in depth behind the listener strip, the same check the moving
    # planner's viewer makes: Envoy APPENDS its projected claim, so more than
    # one value means a forged one arrived first and ordinary header reads
    # would take it.
    projected = request.headers.getlist("x-auth-email")
    if len(projected) != 1:
        raise HTTPException(
            status_code=403,
            detail="missing or ambiguous X-Auth-Email header",
        )
    email = projected[0].strip()
    # An empty allowlist is not "refuse everyone": Access has already decided
    # who reaches this backend, so any verified identity decides. A non-empty
    # one narrows that further.
    if not email or (allowed and email.lower() not in allowed):
        raise HTTPException(status_code=403, detail="not a factory operator")
    return email
