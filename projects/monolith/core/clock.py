from __future__ import annotations

from datetime import datetime, timezone


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso(value: datetime | None) -> str | None:
    coerced = as_utc(value)
    return coerced.isoformat() if coerced is not None else None


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def aware_iso(value: datetime | None) -> str | None:
    return aware(value).isoformat() if value is not None else None


def iso_z(value: datetime | None) -> str | None:
    stamp = iso(value)
    return None if stamp is None else stamp.replace("+00:00", "Z")
