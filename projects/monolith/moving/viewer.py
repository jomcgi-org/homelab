"""Viewer identity resolution for the private moving planner."""

from fastapi import Depends, Header, HTTPException
from sqlmodel import Session, select

from core.db import get_session
from moving.models import Viewer


def get_viewer(
    session: Session = Depends(get_session),
    x_auth_email: list[str] | None = Header(None),
) -> str:
    """Resolve viewer identity from the X-Auth-Email header.

    Deliberately sync, like every path operation in router.py. FastAPI runs a
    ``def`` dependency in the threadpool and an ``async def`` one on the event
    loop, so declaring this async would put the one blocking DB call that runs
    on EVERY request to this domain directly on the loop.

    Gateway-wide ClientTrafficPolicy strips any inbound X-Auth-Email before
    the auth filter, so the only value that can arrive is the one Envoy
    projected from a verified ``family`` group claim. Authorization (may you
    see this at all) already happened at the gateway; this answers only
    "whose view".

    An unknown email is 403 and a missing header is 403, never a default
    viewer. Falling through would silently show one person's plans as the
    other's.
    """
    if not x_auth_email:
        raise HTTPException(status_code=403, detail="Missing X-Auth-Email header")
    # Defence in depth behind the listener's inbound-header strip. Envoy appends
    # its projected identity, so accepting multiple values could trust a forged
    # value that arrived before Envoy's value. This check does not replace the
    # listener strip.
    if len(x_auth_email) != 1:
        raise HTTPException(status_code=403, detail="Ambiguous X-Auth-Email header")

    viewer = session.exec(select(Viewer).where(Viewer.email == x_auth_email[0])).first()
    if not viewer:
        raise HTTPException(status_code=403, detail="Unknown viewer")
    return viewer.name
