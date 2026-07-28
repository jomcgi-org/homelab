"""Out-of-band review and manual tuning for the silent directive autopilot.

The autopilot never announces in Discord, so these functions are the review
surface: other domains reach them through ``chat.api`` (the MCP tools in the
agent domain call them). A manual set, pin, or revert writes the ``manual``
provenance source, which the autopilot treats as a hard precedence winner (it
will not override a manual row within its cooldown). All DB work runs in a
plain session here; the async MCP wrappers dispatch these via a worker thread.
"""

from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, select

from core.db import get_engine
from chat import directives
from chat.models import ChannelDirective, DirectiveAutopilot, UserStylePref


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _latest_autopilot_action(session, scope_kind: str, scope_id: str) -> dict | None:
    row = session.exec(
        select(DirectiveAutopilot)
        .where(DirectiveAutopilot.scope_kind == scope_kind)
        .where(DirectiveAutopilot.scope_id == scope_id)
        .order_by(DirectiveAutopilot.id.desc())
    ).first()
    if row is None:
        return None
    return {
        "status": row.status,
        "rationale": row.rationale,
        "target_version": row.target_version,
        "applied_at": _iso(row.applied_at),
        "created_at": _iso(row.created_at),
    }


def list_directives() -> dict:
    with Session(get_engine()) as session:
        channels = session.exec(
            select(ChannelDirective).where(
                ChannelDirective.active == True  # noqa: E712
            )
        ).all()
        prefs = session.exec(
            select(UserStylePref).where(UserStylePref.active == True)  # noqa: E712
        ).all()
        return {
            "channel_directives": [
                {
                    "channel_id": c.channel_id,
                    "version": c.version,
                    "source": c.source,
                    "directive": c.directive,
                    "latest_autopilot": _latest_autopilot_action(
                        session, "channel", c.channel_id
                    ),
                }
                for c in channels
            ],
            "user_style_prefs": [
                {
                    "user_id": p.user_id,
                    "source": p.source,
                    "pref": p.pref,
                    "latest_autopilot": _latest_autopilot_action(
                        session, "user", p.user_id
                    ),
                }
                for p in prefs
            ],
        }


def directive_history(scope_kind: str, scope_id: str) -> dict:
    if scope_kind not in ("channel", "user"):
        return {"error": "scope_kind must be 'channel' or 'user'"}
    with Session(get_engine()) as session:
        versions: list[dict] = []
        if scope_kind == "user":
            rows = session.exec(
                select(UserStylePref)
                .where(UserStylePref.user_id == scope_id)
                .order_by(UserStylePref.id)
            ).all()
            versions = [
                {
                    "text": r.pref,
                    "active": r.active,
                    "source": r.source,
                    "created_at": _iso(r.created_at),
                }
                for r in rows
            ]
        else:
            rows = session.exec(
                select(ChannelDirective)
                .where(ChannelDirective.channel_id == scope_id)
                .order_by(ChannelDirective.version)
            ).all()
            versions = [
                {
                    "version": r.version,
                    "text": r.directive,
                    "active": r.active,
                    "source": r.source,
                    "proposal_message_id": r.proposal_message_id,
                    "created_at": _iso(r.created_at),
                }
                for r in rows
            ]
        log = session.exec(
            select(DirectiveAutopilot)
            .where(DirectiveAutopilot.scope_kind == scope_kind)
            .where(DirectiveAutopilot.scope_id == scope_id)
            .order_by(DirectiveAutopilot.id)
        ).all()
        return {
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "versions": versions,
            "autopilot_log": [
                {
                    "status": a.status,
                    "target_version": a.target_version,
                    "prior_version": a.prior_version,
                    "rationale": a.rationale,
                    "baseline_json": a.baseline_json,
                    "evidence_json": a.evidence_json,
                    "applied_at": _iso(a.applied_at),
                    "validate_after": _iso(a.validate_after),
                    "created_at": _iso(a.created_at),
                }
                for a in log
            ],
        }


def set_directive(scope_kind: str, scope_id: str, text: str) -> dict:
    if scope_kind not in ("channel", "user"):
        return {"ok": False, "reason": "scope_kind must be 'channel' or 'user'"}
    ok, reason = directives.guard(text)
    if not ok:
        return {"ok": False, "reason": reason}
    if scope_kind == "user":
        directives.set_style_pref(scope_id, text, source="manual")
        return {"ok": True}
    version = directives.set_channel_directive(scope_id, text, source="manual")
    return {"ok": True, "version": version}


def pin_directive(scope_kind: str, scope_id: str) -> dict:
    if scope_kind not in ("channel", "user"):
        return {"ok": False, "reason": "scope_kind must be 'channel' or 'user'"}
    if scope_kind == "user":
        ok = directives.pin_style_pref(scope_id)
    else:
        ok = directives.pin_channel_directive(scope_id)
    return {"ok": ok}


def revert_directive(scope_kind: str, scope_id: str) -> dict:
    if scope_kind not in ("channel", "user"):
        return {"ok": False, "reason": "scope_kind must be 'channel' or 'user'"}
    with Session(get_engine()) as session:
        if scope_kind == "user":
            prior = session.exec(
                select(UserStylePref)
                .where(UserStylePref.user_id == scope_id)
                .where(UserStylePref.active == False)  # noqa: E712
                .order_by(UserStylePref.id.desc())
            ).first()
            if prior is None:
                return {"ok": False, "reason": "no prior version to revert to"}
            prior_text = prior.pref
        else:
            active = session.exec(
                select(ChannelDirective)
                .where(ChannelDirective.channel_id == scope_id)
                .where(ChannelDirective.active == True)  # noqa: E712
            ).first()
            if active is None:
                return {"ok": False, "reason": "no active directive"}
            prior = session.exec(
                select(ChannelDirective)
                .where(ChannelDirective.channel_id == scope_id)
                .where(ChannelDirective.version == active.previous_version)
                .order_by(ChannelDirective.id.desc())
            ).first()
            if prior is None:
                return {"ok": False, "reason": "no prior version to revert to"}
            prior_text = prior.directive

    # Reinstate as a fresh active row, source manual (a human-initiated revert
    # that also wins precedence over the autopilot).
    if scope_kind == "user":
        directives.set_style_pref(scope_id, prior_text, source="manual")
        return {"ok": True}
    version = directives.set_channel_directive(scope_id, prior_text, source="manual")
    return {"ok": True, "version": version}
