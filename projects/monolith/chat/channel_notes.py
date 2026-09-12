"""Per-channel durable notes and rolling-summary configuration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from string import Formatter

from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session

from chat.models import ChannelMemory

EDITABLE_FIELDS = frozenset(
    {"summary_prompt_user", "summary_prompt_channel", "summary_style", "notes"}
)
_FIELD_LIMITS = {
    "summary_prompt_user": 8000,
    "summary_prompt_channel": 8000,
    "summary_style": 500,
    "notes": 12000,
}
_TEMPLATE_FIELDS = {
    "summary_prompt_user": frozenset(
        {"username", "current_summary", "messages", "summary_style", "notes"}
    ),
    "summary_prompt_channel": frozenset(
        {"current_summary", "messages", "summary_style", "notes"}
    ),
}


@dataclass(frozen=True)
class SummaryConfig:
    user_template: str | None = None
    channel_template: str | None = None
    style: str = ""
    notes: str = ""


def get_memory(session: Session, channel_id: str) -> ChannelMemory | None:
    """Return only the row for ``channel_id``."""
    return session.get(ChannelMemory, channel_id)


def load_summary_config(session: Session, channel_id: str) -> SummaryConfig:
    """Load summary settings, returning neutral defaults for a new channel."""
    memory = get_memory(session, channel_id)
    if memory is None:
        return SummaryConfig()
    return SummaryConfig(
        user_template=memory.summary_prompt_user,
        channel_template=memory.summary_prompt_channel,
        style=memory.summary_style or "",
        notes=memory.notes or "",
    )


def validate_update(field: str, value: str) -> str | None:
    """Validate one tool-supplied field and normalize blank text to NULL."""
    if field not in EDITABLE_FIELDS:
        allowed = ", ".join(sorted(EDITABLE_FIELDS))
        raise ValueError(f"field must be one of: {allowed}")
    if not isinstance(value, str):
        raise ValueError("value must be text")
    if len(value) > _FIELD_LIMITS[field]:
        raise ValueError(f"{field} exceeds {_FIELD_LIMITS[field]} characters")
    if "\x00" in value:
        raise ValueError("value cannot contain NUL characters")
    normalized = value.strip()
    if field in _TEMPLATE_FIELDS and normalized:
        validate_template(field, normalized)
    return normalized or None


def validate_template(field: str, template: str) -> None:
    """Allow simple named fields only, never attribute/index traversal or specs."""
    allowed = _TEMPLATE_FIELDS[field]
    seen: set[str] = set()
    try:
        parts = Formatter().parse(template)
        for _literal, field_name, format_spec, conversion in parts:
            if field_name is None:
                continue
            if field_name not in allowed:
                raise ValueError(
                    f"unsupported placeholder {{{field_name}}}; allowed: "
                    + ", ".join(f"{{{name}}}" for name in sorted(allowed))
                )
            if format_spec or conversion:
                raise ValueError(
                    "format specifications and conversions are not allowed"
                )
            seen.add(field_name)
    except (KeyError, IndexError) as exc:
        raise ValueError("invalid prompt template") from exc
    if "messages" not in seen:
        raise ValueError("prompt templates must include {messages}")


def render_template(field: str, template: str, values: dict[str, str]) -> str:
    """Render a previously validated template from a fixed string-only mapping."""
    validate_template(field, template)
    allowed = _TEMPLATE_FIELDS[field]
    return template.format_map({name: str(values.get(name, "")) for name in allowed})


def update_memory(
    session: Session,
    channel_id: str,
    field: str,
    value: str,
    updated_by_user_id: str,
) -> ChannelMemory:
    """Atomically upsert one field, preserving concurrent updates to other fields."""
    if not channel_id or len(channel_id) > 64:
        raise ValueError("channel_id must contain at most 64 characters")
    if len(updated_by_user_id) > 64:
        raise ValueError("updated_by_user_id must contain at most 64 characters")
    normalized = validate_update(field, value)
    now = datetime.now(timezone.utc)
    values = {
        "channel_id": channel_id,
        field: normalized,
        "updated_by_user_id": updated_by_user_id,
        "updated_at": now,
    }
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        stmt = postgres_insert(ChannelMemory).values(**values)
    elif dialect == "sqlite":
        stmt = sqlite_insert(ChannelMemory).values(**values)
    else:
        raise RuntimeError(f"channel memory does not support {dialect}")
    stmt = stmt.on_conflict_do_update(
        index_elements=[ChannelMemory.channel_id],
        set_={
            field: normalized,
            "updated_by_user_id": updated_by_user_id,
            "updated_at": now,
        },
    )
    session.exec(stmt)
    session.commit()
    memory = get_memory(session, channel_id)
    if memory is None:  # pragma: no cover - the upsert guarantees this row
        raise RuntimeError("channel memory upsert did not return a row")
    return memory


def format_memory(memory: ChannelMemory | None) -> str:
    """Render the current channel settings for a tool response."""
    if memory is None:
        return "This channel has no saved notes or summary configuration."
    lines = ["Channel memory:"]
    for field in sorted(EDITABLE_FIELDS):
        value = getattr(memory, field)
        lines.append(f"- {field}: {value if value else '(default)'}")
    return "\n".join(lines)
