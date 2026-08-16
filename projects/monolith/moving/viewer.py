"""Viewer identity resolution for the private moving planner."""

from fastapi import Depends, Header, HTTPException
from sqlmodel import Session, select

from core.db import get_session
from moving.models import Viewer


async def get_viewer(
    session: Session = Depends(get_session),
    x_auth_email: str | None = Header(None),
) -> str:
    """Resolve viewer identity from the X-Auth-Email header.

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

    viewer = session.exec(select(Viewer).where(Viewer.email == x_auth_email)).first()
    if not viewer:
        raise HTTPException(status_code=403, detail="Unknown viewer")
    return viewer.name
