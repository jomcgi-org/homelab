"""Stars scheduled job handlers (refresh + prune). Fleshed out in later tasks."""

from sqlmodel import Session


async def refresh_handler(session: Session) -> None:  # implemented in a later task
    raise NotImplementedError


def prune_hours_handler(session: Session) -> None:  # implemented in a later task
    raise NotImplementedError
