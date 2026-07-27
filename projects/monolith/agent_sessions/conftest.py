"""Pytest configuration for agent_sessions tests."""

import sqlite3
from datetime import datetime, timezone

# Python 3.13 removed the default sqlite3 datetime adapter, so register one
# to handle binding tz-aware datetimes in tests


def _sqlite3_adapt_datetime(dt):
    """Adapt datetime to SQLite format."""
    return dt.isoformat() if isinstance(dt, datetime) else dt


def _sqlite3_convert_datetime(s):
    """Convert SQLite datetime back to Python datetime."""
    if isinstance(s, bytes):
        s = s.decode("utf-8")
    # Try to parse as ISO format datetime with timezone
    try:
        # First try with timezone
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        # Fall back to naive datetime if no timezone
        try:
            return datetime.fromisoformat(s.split("+")[0].split("Z")[0])
        except (ValueError, AttributeError):
            return s


# Register adapters and converters
sqlite3.register_adapter(datetime, _sqlite3_adapt_datetime)
sqlite3.register_converter("TIMESTAMP", _sqlite3_convert_datetime)
sqlite3.register_converter("TIMESTAMPTZ", _sqlite3_convert_datetime)
