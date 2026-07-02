"""Generic per-server Discord bot feature ACL (ADR 029).

Grants live in ``chat.discord_feature_grant`` and are allow-list only: an action
is permitted iff a matching grant row exists. Empty-string sentinels are
wildcards, ``guild_id == ""`` matches any server, ``subject_id == ""`` matches
every user in a server, and ``scope == ""`` grants the whole feature. For the
``agent`` feature, scope is the repo name.

All functions are synchronous (open their own session); call via
``asyncio.to_thread`` from the bot's async handlers.
"""

from __future__ import annotations

import logging
import os

from sqlmodel import Session, select

from app.db import get_engine
from chat.models import DiscordFeatureGrant

logger = logging.getLogger(__name__)

# The Loom server is opted in by default so the collaborator can use /agent on
# the private loom repo out of the box (ADR 029 live target). Other servers are
# opted in by inserting grant rows.
LOOM_GUILD_ID = "1512814732392927463"


def _norm(value: object) -> str:
    """Discord ids arrive as int or str; grants store strings, "" for absent."""
    return "" if value is None else str(value)


def _grants_for_feature(feature: str) -> list[DiscordFeatureGrant]:
    with Session(get_engine()) as session:
        return list(
            session.exec(
                select(DiscordFeatureGrant).where(
                    DiscordFeatureGrant.feature == feature
                )
            ).all()
        )


def is_granted(
    guild_id: object, subject_id: object, feature: str, scope: str = ""
) -> bool:
    """True when a grant matches, honoring "" wildcards on guild/subject/scope.

    A ``scope`` argument of "" checks feature-wide access (any grant for the
    feature in this guild/subject). A non-empty ``scope`` requires a grant whose
    scope is that value or "" (a whole-feature grant).
    """
    gid, uid = _norm(guild_id), _norm(subject_id)
    for row in _grants_for_feature(feature):
        if row.guild_id not in ("", gid):
            continue
        if row.subject_id not in ("", uid):
            continue
        if scope and row.scope not in ("", scope):
            continue
        return True
    return False


def feature_enabled(guild_id: object, feature: str) -> bool:
    """True when the feature has any grant in this server (any subject/scope)."""
    gid = _norm(guild_id)
    return any(row.guild_id in ("", gid) for row in _grants_for_feature(feature))


def allowed_scopes(guild_id: object, subject_id: object, feature: str) -> set[str]:
    """The non-empty scopes granted to this guild/subject for the feature.

    For ``agent`` this is the set of repos the caller may run in this server,
    used to validate the repo the command was invoked with.
    """
    gid, uid = _norm(guild_id), _norm(subject_id)
    return {
        row.scope
        for row in _grants_for_feature(feature)
        if row.guild_id in ("", gid) and row.subject_id in ("", uid) and row.scope
    }


def bootstrap_defaults() -> None:
    """Idempotently seed the baked-in default grants (ADR 029).

    Reads the existing owner/home-server env so the home server always works out
    of the box: the home server gets /agent on the public homelab repo for
    everyone, the owner additionally gets /agent on the private loom repo and
    /artifact, and the Loom server gets /agent on loom for its members. Safe to
    call on every startup; existing rows are left untouched.
    """
    home = os.environ.get("MONOLITH_AGENT_DISCORD_DEFAULT_SERVER_ID", "")
    owner = os.environ.get("OWNER_DISCORD_USER_ID", "")

    defaults: list[tuple[str, str, str, str]] = []
    if home:
        # homelab is public: anyone in the home server may run /agent on it.
        defaults.append((home, "", "agent", "homelab"))
        if owner:
            # loom is private: from the home server only the owner gets it, so a
            # home-server member cannot drive the private repo. Loom-server
            # members get loom via the LOOM_GUILD_ID grant below.
            defaults.append((home, owner, "agent", "loom"))
            defaults.append((home, owner, "artifact", ""))
    defaults.append((LOOM_GUILD_ID, "", "agent", "loom"))

    to_add: list[DiscordFeatureGrant] = []
    with Session(get_engine()) as session:
        to_add = [
            DiscordFeatureGrant(
                guild_id=gid, subject_id=uid, feature=feature, scope=scope
            )
            for gid, uid, feature, scope in defaults
            if session.get(DiscordFeatureGrant, (gid, uid, feature, scope)) is None
        ]
        if to_add:
            session.add_all(to_add)
            session.commit()
    if to_add:
        logger.info("acl: seeded %d default Discord feature grants", len(to_add))
