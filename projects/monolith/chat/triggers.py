"""Validated persistence and atomic matching for Discord message triggers."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import regex
from core.db import get_engine
from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from chat.models import MessageTrigger

logger = logging.getLogger(__name__)

MAX_PATTERN_LENGTH = 512
MAX_FILTER_IDS = 50
MAX_TRIGGER_COUNT = 100
MAX_COOLDOWN_SECS = 7 * 24 * 60 * 60
MAX_MATCH_CONTENT = 4000
MATCH_TIMEOUT_SECS = 0.02

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.:-]{0,99}$")
_TEMPLATE_FIELD_RE = re.compile(r"\{(?:content|author|channel_id)\}")
# Nested repetition is a common catastrophic backtracking shape. Runtime
# matching is independently timeout-bounded for other ambiguous patterns.
_NESTED_REPEAT_RE = re.compile(
    r"\([^()]*(?:[*+]|\{\d+(?:,\d*)?\})[^()]*\)"
    r"(?:[*+]|\{\d+(?:,\d*)?\})"
)


class TriggerValidationError(ValueError):
    """A trigger configuration is invalid and safe to show to the caller."""


@dataclass(frozen=True)
class TriggerClaim:
    """Detached action data for a trigger whose cooldown has been claimed."""

    id: int
    name: str
    action_type: str
    action_config: dict[str, Any]
    created_by_user_id: str


@dataclass(frozen=True)
class _TriggerCandidate:
    id: int
    name: str
    pattern: str
    channel_ids: list[str]
    user_ids: list[str]
    action_type: str
    action_config: dict[str, Any]
    cooldown_secs: int
    created_by_user_id: str


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def validate_pattern(pattern: object) -> str:
    """Compile a bounded pattern and reject common exponential-time shapes."""
    if not isinstance(pattern, str) or not pattern:
        raise TriggerValidationError("pattern must be a non-empty string")
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise TriggerValidationError(
            f"pattern must be at most {MAX_PATTERN_LENGTH} characters"
        )
    if _NESTED_REPEAT_RE.search(pattern):
        raise TriggerValidationError(
            "pattern contains nested repetition that can backtrack excessively"
        )
    try:
        compiled = regex.compile(pattern, regex.IGNORECASE | regex.VERSION1)
    except regex.error as exc:
        raise TriggerValidationError(f"invalid regex: {exc}") from exc

    # Exercise typical adversarial non-matches at write time. The same timeout
    # is enforced against real messages, so an exotic costly pattern is still
    # isolated even when these probes do not expose it.
    for character in ("a", "0", " "):
        probe = character * (MAX_MATCH_CONTENT - 1) + "!"
        try:
            compiled.search(probe, timeout=MATCH_TIMEOUT_SECS)
        except TimeoutError as exc:
            raise TriggerValidationError(
                "pattern exceeded the regex execution limit"
            ) from exc
    return pattern


def _validate_name(name: object) -> str:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name.strip()):
        raise TriggerValidationError(
            "name must be 1-100 characters using letters, numbers, spaces, or ._:-"
        )
    return name.strip()


def _validate_ids(values: object, label: str) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, (list, tuple)):
        raise TriggerValidationError(f"{label} must be a list")
    normalized: list[str] = []
    for value in values:
        item = str(value).strip()
        if not item or len(item) > 64 or not item.isdigit():
            raise TriggerValidationError(
                f"each {label} entry must be a numeric Discord ID up to 64 characters"
            )
        if item not in normalized:
            normalized.append(item)
    if len(normalized) > MAX_FILTER_IDS:
        raise TriggerValidationError(
            f"{label} cannot contain more than {MAX_FILTER_IDS} IDs"
        )
    return normalized


def _validate_cooldown(value: object) -> int:
    if isinstance(value, bool):
        raise TriggerValidationError("cooldown_secs must be an integer")
    try:
        cooldown = int(value)
    except (TypeError, ValueError) as exc:
        raise TriggerValidationError("cooldown_secs must be an integer") from exc
    if cooldown < 0 or cooldown > MAX_COOLDOWN_SECS:
        raise TriggerValidationError(
            f"cooldown_secs must be between 0 and {MAX_COOLDOWN_SECS}"
        )
    return cooldown


def _content(config: dict[str, Any], required: bool) -> str | None:
    value = config.get("content", config.get("message"))
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise TriggerValidationError("action content must be a non-empty string")
    if len(value) > 2000:
        raise TriggerValidationError("action content must be at most 2000 characters")
    return value.strip()


def validate_action(
    action_type: object, action_config: object
) -> tuple[str, dict[str, Any]]:
    """Return a canonical action name and payload, rejecting unknown fields."""
    if not isinstance(action_type, str):
        raise TriggerValidationError(
            "action_type must be respond, crosspost, or agent_run"
        )
    canonical = action_type.strip().lower().replace("-", "_")
    if canonical not in {"respond", "crosspost", "agent_run"}:
        raise TriggerValidationError(
            "action_type must be respond, crosspost, or agent_run"
        )
    if not isinstance(action_config, dict):
        raise TriggerValidationError("action_config must be an object")

    if canonical == "respond":
        unknown = set(action_config) - {"content", "message"}
        if unknown:
            raise TriggerValidationError(
                f"unknown respond action fields: {', '.join(sorted(unknown))}"
            )
        return canonical, {"content": _content(action_config, required=True)}

    if canonical == "crosspost":
        unknown = set(action_config) - {
            "target_channel_id",
            "channel_id",
            "content",
            "message",
        }
        if unknown:
            raise TriggerValidationError(
                f"unknown crosspost action fields: {', '.join(sorted(unknown))}"
            )
        target = action_config.get("target_channel_id", action_config.get("channel_id"))
        targets = _validate_ids(
            [target] if target is not None else [], "target channel"
        )
        if len(targets) != 1:
            raise TriggerValidationError("crosspost requires target_channel_id")
        result: dict[str, Any] = {"target_channel_id": targets[0]}
        content = _content(action_config, required=False)
        if content is not None:
            result["content"] = content
        return canonical, result

    unknown = set(action_config) - {"prompt", "repo", "model"}
    if unknown:
        raise TriggerValidationError(
            f"unknown agent_run action fields: {', '.join(sorted(unknown))}"
        )
    prompt = action_config.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise TriggerValidationError("agent_run requires a non-empty prompt")
    if len(prompt) > 4000:
        raise TriggerValidationError("agent_run prompt must be at most 4000 characters")
    repo = action_config.get("repo", "")
    if not isinstance(repo, str) or len(repo) > 200:
        raise TriggerValidationError(
            "agent_run repo must be a string up to 200 characters"
        )
    model = action_config.get("model", "luna")
    # Keep durable trigger configuration aligned with the model tiers accepted
    # by agent sessions. The import stays local to avoid loading that package
    # for respond and crosspost validation.
    from agent_sessions import SUPPORTED_MODELS

    if not isinstance(model, str) or model not in SUPPORTED_MODELS:
        raise TriggerValidationError("agent_run model is invalid")
    return canonical, {"prompt": prompt.strip(), "repo": repo.strip(), "model": model}


def create_trigger(
    session: Session,
    *,
    name: object,
    pattern: object,
    channel_ids: object,
    user_ids: object,
    action_type: object,
    action_config: object,
    cooldown_secs: object = 0,
    enabled: bool = True,
    created_by_user_id: str = "",
) -> MessageTrigger:
    """Validate and add a trigger. The caller commits."""
    current = session.exec(select(MessageTrigger.id)).all()
    if len(current) >= MAX_TRIGGER_COUNT:
        raise TriggerValidationError(
            f"at most {MAX_TRIGGER_COUNT} triggers are allowed"
        )
    canonical_action, canonical_config = validate_action(action_type, action_config)
    trigger = MessageTrigger(
        name=_validate_name(name),
        pattern=validate_pattern(pattern),
        channel_ids=_validate_ids(channel_ids, "channel_ids"),
        user_ids=_validate_ids(user_ids, "user_ids"),
        action_type=canonical_action,
        action_config=canonical_config,
        cooldown_secs=_validate_cooldown(cooldown_secs),
        enabled=bool(enabled),
        created_by_user_id=str(created_by_user_id),
    )
    session.add(trigger)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise TriggerValidationError(
            f"a trigger named {trigger.name!r} already exists"
        ) from exc
    return trigger


def list_triggers(session: Session) -> list[MessageTrigger]:
    return list(
        session.exec(select(MessageTrigger).order_by(MessageTrigger.name)).all()
    )


def _get_by_name(session: Session, name: object) -> MessageTrigger:
    clean_name = _validate_name(name)
    trigger = session.exec(
        select(MessageTrigger).where(MessageTrigger.name == clean_name)
    ).first()
    if trigger is None:
        raise TriggerValidationError(f"trigger {clean_name!r} was not found")
    return trigger


def update_trigger(
    session: Session,
    name: object,
    *,
    pattern: object = None,
    channel_ids: object = None,
    user_ids: object = None,
    action_type: object = None,
    action_config: object = None,
    cooldown_secs: object = None,
    enabled: bool | None = None,
) -> MessageTrigger:
    """Validate and update supplied fields on a named trigger. Caller commits."""
    trigger = _get_by_name(session, name)
    if pattern is not None:
        trigger.pattern = validate_pattern(pattern)
    if channel_ids is not None:
        trigger.channel_ids = _validate_ids(channel_ids, "channel_ids")
    if user_ids is not None:
        trigger.user_ids = _validate_ids(user_ids, "user_ids")
    if action_type is not None or action_config is not None:
        canonical_action, canonical_config = validate_action(
            action_type if action_type is not None else trigger.action_type,
            action_config if action_config is not None else trigger.action_config,
        )
        trigger.action_type = canonical_action
        trigger.action_config = canonical_config
    if cooldown_secs is not None:
        trigger.cooldown_secs = _validate_cooldown(cooldown_secs)
    if enabled is not None:
        trigger.enabled = bool(enabled)
    trigger.updated_at = datetime.now(timezone.utc)
    session.add(trigger)
    session.flush()
    return trigger


def set_enabled(session: Session, name: object, enabled: bool) -> MessageTrigger:
    return update_trigger(session, name, enabled=enabled)


def delete_trigger(session: Session, name: object) -> MessageTrigger:
    trigger = _get_by_name(session, name)
    session.delete(trigger)
    session.flush()
    return trigger


def _matches(
    trigger: MessageTrigger | _TriggerCandidate,
    channel_id: str,
    user_id: str,
    content: str,
) -> bool:
    if not isinstance(trigger.channel_ids, list) or not isinstance(
        trigger.user_ids, list
    ):
        logger.warning("trigger %s has malformed filters; skipping", trigger.id)
        return False
    if trigger.channel_ids and channel_id not in trigger.channel_ids:
        return False
    if trigger.user_ids and user_id not in trigger.user_ids:
        return False
    try:
        return bool(
            regex.search(
                trigger.pattern,
                content[:MAX_MATCH_CONTENT],
                regex.IGNORECASE | regex.VERSION1,
                timeout=MATCH_TIMEOUT_SECS,
            )
        )
    except (regex.error, TimeoutError):
        logger.warning("trigger %s regex failed or timed out; skipping", trigger.id)
        return False


def claim_matching(
    session: Session,
    channel_id: str,
    user_id: str,
    content: str,
    *,
    now: datetime | None = None,
) -> list[TriggerClaim]:
    """Atomically claim matching triggers whose cooldown has expired.

    The conditional UPDATE is the concurrency boundary: two replicas may read
    the same candidate, but only one can move last_fired_at past the old cutoff.
    Claims commit before action dispatch, preventing retries or pod restarts
    from accidentally repeating a side effect.
    """
    claimed_at = _utc(now or datetime.now(timezone.utc))
    rows = session.exec(
        select(MessageTrigger)
        .where(MessageTrigger.enabled == True)
        .order_by(MessageTrigger.id)
    ).all()
    candidates = [
        _TriggerCandidate(
            id=row.id,
            name=row.name,
            pattern=row.pattern,
            channel_ids=list(row.channel_ids),
            user_ids=list(row.user_ids),
            action_type=row.action_type,
            action_config=dict(row.action_config),
            cooldown_secs=row.cooldown_secs,
            created_by_user_id=row.created_by_user_id,
        )
        for row in rows
        if row.id is not None
    ]
    # End the read transaction before conditional writes. This avoids a
    # SQLite read-to-write lock upgrade race in hermetic contention tests and
    # leaves the production Postgres UPDATE as the sole claim boundary.
    session.rollback()
    claims: list[TriggerClaim] = []
    for trigger in candidates:
        if not _matches(trigger, channel_id, user_id, content):
            continue
        statement = (
            update(MessageTrigger)
            .where(MessageTrigger.id == trigger.id)
            .where(MessageTrigger.enabled == True)
        )
        if trigger.cooldown_secs:
            cutoff = claimed_at - timedelta(seconds=trigger.cooldown_secs)
            statement = statement.where(
                or_(
                    MessageTrigger.last_fired_at.is_(None),
                    MessageTrigger.last_fired_at <= cutoff,
                )
            )
        result = session.execute(statement.values(last_fired_at=claimed_at))
        if result.rowcount == 1:
            claims.append(
                TriggerClaim(
                    id=trigger.id,
                    name=trigger.name,
                    action_type=trigger.action_type,
                    action_config=dict(trigger.action_config),
                    created_by_user_id=trigger.created_by_user_id,
                )
            )
    session.commit()
    return claims


def claim_matching_for_message(
    channel_id: str, user_id: str, content: str
) -> list[TriggerClaim]:
    """Open the production session used by the async Discord handler."""
    with Session(get_engine()) as session:
        return claim_matching(session, channel_id, user_id, content)


def manage_triggers(
    operation: str,
    *,
    author_id: str,
    current_channel_id: str,
    name: str = "",
    pattern: str | None = None,
    channel_ids: list[str] | None = None,
    user_ids: list[str] | None = None,
    action_type: str | None = None,
    action_config: dict[str, Any] | None = None,
    cooldown_secs: int | None = None,
    enabled: bool | None = None,
) -> str:
    """Session-owning CRUD adapter used by the PydanticAI tool."""
    op = (operation or "").strip().lower()
    with Session(get_engine()) as session:
        if op == "list":
            rows = list_triggers(session)
            if not rows:
                return "No message triggers are configured."
            lines = ["Configured message triggers:"]
            for row in rows:
                state = "enabled" if row.enabled else "disabled"
                channels = ",".join(row.channel_ids) if row.channel_ids else "all"
                users = ",".join(row.user_ids) if row.user_ids else "all"
                lines.append(
                    f"- {row.name} (#{row.id}, {state}, {row.action_type}, "
                    f"channels={channels}, users={users}, cooldown={row.cooldown_secs}s)"
                )
            return "\n".join(lines)

        if op == "create":
            if pattern is None or action_type is None or action_config is None:
                raise TriggerValidationError(
                    "create requires name, pattern, action_type, and action_config"
                )
            row = create_trigger(
                session,
                name=name,
                pattern=pattern,
                channel_ids=[current_channel_id]
                if channel_ids is None
                else channel_ids,
                user_ids=[] if user_ids is None else user_ids,
                action_type=action_type,
                action_config=action_config,
                cooldown_secs=0 if cooldown_secs is None else cooldown_secs,
                enabled=True if enabled is None else enabled,
                created_by_user_id=author_id,
            )
            session.commit()
            return f"Created trigger {row.name!r} (#{row.id})."

        if op == "update":
            row = update_trigger(
                session,
                name,
                pattern=pattern,
                channel_ids=channel_ids,
                user_ids=user_ids,
                action_type=action_type,
                action_config=action_config,
                cooldown_secs=cooldown_secs,
                enabled=enabled,
            )
            session.commit()
            return f"Updated trigger {row.name!r}."
        if op in {"enable", "disable"}:
            row = set_enabled(session, name, op == "enable")
            session.commit()
            return f"{op.title()}d trigger {row.name!r}."
        if op == "delete":
            row = delete_trigger(session, name)
            row_name = row.name
            session.commit()
            return f"Deleted trigger {row_name!r}."
        raise TriggerValidationError(
            "operation must be create, list, update, enable, disable, or delete"
        )


def render_template(
    template: str,
    *,
    content: str,
    author: str,
    channel_id: str,
    max_length: int = 2000,
) -> str:
    """Expand the deliberately small trigger template vocabulary."""
    values = {
        "{content}": content,
        "{author}": author,
        "{channel_id}": channel_id,
    }
    rendered = _TEMPLATE_FIELD_RE.sub(lambda match: values[match.group()], template)
    return rendered[:max_length]
