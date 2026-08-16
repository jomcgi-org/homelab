from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from uuid import uuid4

from sqlmodel import Session

from agent_sessions import store
from agent_sessions.models import AgentSession
from core.db import get_engine

SURFACES = frozenset({"run", "walkthrough", "transcript", "vm"})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def register_companion(
    companion_id: str | None,
    principal_subject: str,
    principal_authority: str,
) -> str:
    with Session(get_engine()) as session:
        if companion_id is not None:
            existing = store.get_voice_ui_companion(session, companion_id)
            if existing is not None:
                store.heartbeat_voice_ui_companion(
                    session,
                    existing,
                    _now(),
                    principal_subject,
                    principal_authority,
                )
                return existing.id
        minted_id = str(uuid4())
        store.create_voice_ui_companion(
            session,
            minted_id,
            principal_subject,
            principal_authority,
            _now(),
        )
        return minted_id


def poll_ledger(companion_id: str, since: int) -> list[dict] | None:
    with Session(get_engine()) as session:
        return store.poll_voice_ui_ledger(session, companion_id, since, _now())


def attach(
    session_id: int | None,
    principal_subject: str,
    principal_authority: str,
    mint_session: Callable[[], AgentSession],
) -> dict:
    with Session(get_engine()) as session:
        companion = store.get_open_voice_ui_companion(session, _now(), for_update=True)
        if companion is None:
            return {"accepted": True, "companion_open": False}

        resolved_session_id = session_id
        if resolved_session_id is None:
            resolved_session_id = companion.session_id
            if resolved_session_id is None:
                minted = mint_session()
                resolved_session_id = minted.id

        store.record_voice_ui_call(
            session,
            companion,
            "attach",
            {"session_id": resolved_session_id},
            principal_subject,
            principal_authority,
            bound_session_id=resolved_session_id,
        )
        return {
            "accepted": True,
            "session_id": resolved_session_id,
            "companion_open": True,
        }


def show(
    surface: str,
    ref: str,
    focus: str | None,
    principal_subject: str,
    principal_authority: str,
) -> dict:
    if surface not in SURFACES:
        return {
            "accepted": False,
            "error": _surface_error(surface),
            "companion_open": True,
        }
    with Session(get_engine()) as session:
        companion = store.get_open_voice_ui_companion(session, _now())
        if companion is None:
            # Degrade to voice: a screen is optional, so no open companion means
            # acceptance without a ledger row or any other side effect.
            return {"accepted": True, "companion_open": False}
        store.record_voice_ui_call(
            session,
            companion,
            "show",
            {"surface": surface, "ref": ref, "focus": focus},
            principal_subject,
            principal_authority,
        )
        return {"accepted": True, "companion_open": True}


def ask(
    question: str,
    options: list[str],
    ref: str,
    principal_subject: str,
    principal_authority: str,
) -> dict:
    return _record_if_open(
        "ask",
        {"question": question, "options": options, "ref": ref},
        principal_subject,
        principal_authority,
    )


def dismiss(
    surface: str | None,
    principal_subject: str,
    principal_authority: str,
) -> dict:
    if surface is not None and surface not in SURFACES:
        return {
            "accepted": False,
            "error": _surface_error(surface),
            "companion_open": True,
        }
    with Session(get_engine()) as session:
        companion = store.get_open_voice_ui_companion(session, _now())
        if companion is None:
            return {"accepted": True, "companion_open": False}
        store.record_voice_ui_call(
            session,
            companion,
            "dismiss",
            {"surface": surface},
            principal_subject,
            principal_authority,
        )
        return {"accepted": True, "companion_open": True}


def _record_if_open(
    call: str,
    payload: dict,
    principal_subject: str,
    principal_authority: str,
) -> dict:
    with Session(get_engine()) as session:
        companion = store.get_open_voice_ui_companion(session, _now())
        if companion is None:
            return {"accepted": True, "companion_open": False}
        store.record_voice_ui_call(
            session,
            companion,
            call,
            payload,
            principal_subject,
            principal_authority,
        )
        return {"accepted": True, "companion_open": True}


def _surface_error(surface: str) -> str:
    return f"unknown surface {surface}; valid surfaces: {', '.join(sorted(SURFACES))}"
