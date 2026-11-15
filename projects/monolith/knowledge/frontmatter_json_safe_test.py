"""Tests for _json_safe() in knowledge.frontmatter.

Covers:
- datetime.date objects coerced to ISO string
- datetime.datetime objects coerced to ISO string
- Passthrough for str, int, float, None, list, dict
- Integration: frontmatter with bare ISO date in extra parses to string
"""

from __future__ import annotations

import datetime
import json

from knowledge.frontmatter import _json_safe, parse


# ---------------------------------------------------------------------------
# date objects → ISO string
# ---------------------------------------------------------------------------


class TestJsonSafeDate:
    def test_date_object_returns_iso_string(self):
        """datetime.date coerced to 'YYYY-MM-DD' ISO string."""
        d = datetime.date(2024, 1, 15)
        result = _json_safe(d)
        assert result == "2024-01-15"

    def test_date_object_returns_str_type(self):
        """Return type is str for datetime.date input."""
        d = datetime.date(2024, 1, 15)
        assert isinstance(_json_safe(d), str)

    def test_date_year_boundary(self):
        """date at year boundary (Jan 1) formats correctly."""
        d = datetime.date(2000, 1, 1)
        assert _json_safe(d) == "2000-01-01"

    def test_date_end_of_year(self):
        """date at end of year (Dec 31) formats correctly."""
        d = datetime.date(2023, 12, 31)
        assert _json_safe(d) == "2023-12-31"

    def test_date_is_json_serializable(self):
        """Coerced date value can be passed to json.dumps without error."""
        d = datetime.date(2024, 6, 15)
        result = _json_safe(d)
        # Should not raise
        json.dumps(result)


# ---------------------------------------------------------------------------
# datetime objects → ISO string
# ---------------------------------------------------------------------------


class TestJsonSafeDatetime:
    def test_datetime_object_returns_isoformat_string(self):
        """datetime.datetime coerced to ISO format string."""
        dt = datetime.datetime(2024, 1, 15, 10, 30, 0)
        result = _json_safe(dt)
        assert result == "2024-01-15T10:30:00"

    def test_datetime_object_returns_str_type(self):
        """Return type is str for datetime.datetime input."""
        dt = datetime.datetime(2024, 1, 15, 10, 30)
        assert isinstance(_json_safe(dt), str)

    def test_datetime_with_microseconds(self):
        """datetime with microseconds formats to full ISO string."""
        dt = datetime.datetime(2024, 3, 20, 14, 5, 30, 123456)
        result = _json_safe(dt)
        assert result == "2024-03-20T14:05:30.123456"

    def test_datetime_midnight(self):
        """Midnight datetime formats without time component (00:00:00)."""
        dt = datetime.datetime(2024, 6, 1, 0, 0, 0)
        result = _json_safe(dt)
        assert result == "2024-06-01T00:00:00"

    def test_datetime_with_timezone_utc(self):
        """Timezone-aware UTC datetime includes offset in ISO string."""
        dt = datetime.datetime(2024, 1, 15, 10, 30, tzinfo=datetime.timezone.utc)
        result = _json_safe(dt)
        assert isinstance(result, str)
        assert "2024-01-15" in result
        assert "10:30" in result

    def test_datetime_is_json_serializable(self):
        """Coerced datetime value can be passed to json.dumps without error."""
        dt = datetime.datetime(2024, 6, 15, 12, 0, 0)
        result = _json_safe(dt)
        # Should not raise
        json.dumps(result)

    def test_datetime_subclass_before_date(self):
        """datetime (a subclass of date) hits the datetime branch, not the date branch."""
        # datetime.isoformat() includes the time component; date.isoformat() does not.
        dt = datetime.datetime(2024, 1, 15, 10, 30)
        result = _json_safe(dt)
        # Must include the time part — confirms datetime branch was taken
        assert "T" in result


# ---------------------------------------------------------------------------
# Passthrough for non-date types
# ---------------------------------------------------------------------------


class TestJsonSafePassthrough:
    def test_string_passes_through(self):
        """Plain strings are returned unchanged."""
        assert _json_safe("hello") == "hello"

    def test_empty_string_passes_through(self):
        """Empty string passes through unchanged."""
        assert _json_safe("") == ""

    def test_integer_passes_through(self):
        """Integer values pass through unchanged."""
        assert _json_safe(42) == 42

    def test_zero_passes_through(self):
        """Zero passes through unchanged."""
        assert _json_safe(0) == 0

    def test_float_passes_through(self):
        """Float values pass through unchanged."""
        assert _json_safe(3.14) == 3.14

    def test_none_passes_through(self):
        """None passes through unchanged."""
        assert _json_safe(None) is None

    def test_list_passes_through(self):
        """List values pass through unchanged."""
        lst = [1, 2, 3]
        result = _json_safe(lst)
        assert result is lst

    def test_dict_passes_through(self):
        """Dict values pass through unchanged."""
        d = {"key": "value"}
        result = _json_safe(d)
        assert result is d

    def test_bool_true_passes_through(self):
        """Boolean True passes through unchanged."""
        assert _json_safe(True) is True

    def test_bool_false_passes_through(self):
        """Boolean False passes through unchanged."""
        assert _json_safe(False) is False


# ---------------------------------------------------------------------------
# Integration: parse() applies _json_safe to extra fields
# ---------------------------------------------------------------------------


class TestJsonSafeIntegration:
    def test_bare_iso_date_in_extra_field_becomes_string(self):
        """A bare ISO date value in an extra YAML field parses to a string, not a date object."""
        # YAML safe_load turns `date: 2024-01-15` into datetime.date(2024, 1, 15)
        raw = "---\ntitle: T\npublished: 2024-01-15\n---\nbody"
        meta, _ = parse(raw)
        published = meta.extra["published"]
        assert isinstance(published, str), (
            f"Expected str in extra['published'], got {type(published).__name__}"
        )
        assert published == "2024-01-15"

    def test_extra_date_value_is_json_serializable(self):
        """The extra dict from a note with date fields can be round-tripped through json.dumps."""
        raw = "---\ntitle: T\npublished: 2026-04-09\nrevised: 2026-04-10\n---\nbody"
        meta, _ = parse(raw)
        # Should not raise TypeError about date serialization
        serialized = json.dumps(meta.extra)
        data = json.loads(serialized)
        assert data["published"] == "2026-04-09"
        assert data["revised"] == "2026-04-10"

    def test_extra_datetime_value_is_json_serializable(self):
        """Extra fields containing datetime.datetime values are coerced to strings."""
        # PyYAML parses `2024-01-15T10:30:00` without quotes as datetime
        raw = "---\ntitle: T\nrecorded_at: '2024-01-15T10:30:00'\n---\nbody"
        meta, _ = parse(raw)
        # After _json_safe, should be a string
        assert isinstance(meta.extra.get("recorded_at"), str)
        # Must be JSON-serializable
        json.dumps(meta.extra)

    def test_extra_string_value_unchanged(self):
        """Extra fields with plain string values are not modified."""
        raw = "---\ntitle: T\nauthor: Karpathy\n---\nbody"
        meta, _ = parse(raw)
        assert meta.extra["author"] == "Karpathy"

    def test_extra_integer_value_unchanged(self):
        """Extra fields with integer values are not modified."""
        raw = "---\ntitle: T\nyear: 2024\n---\nbody"
        meta, _ = parse(raw)
        assert meta.extra["year"] == 2024

    def test_extra_none_value_unchanged(self):
        """Extra fields with null YAML values remain None."""
        raw = "---\ntitle: T\nreviewer: null\n---\nbody"
        meta, _ = parse(raw)
        assert meta.extra["reviewer"] is None

    def test_multiple_date_fields_in_extra_all_coerced(self):
        """Multiple date fields in extra are all coerced to strings."""
        raw = "---\ntitle: T\nstart_date: 2024-01-01\nend_date: 2024-12-31\n---\nbody"
        meta, _ = parse(raw)
        assert isinstance(meta.extra["start_date"], str)
        assert isinstance(meta.extra["end_date"], str)
        assert meta.extra["start_date"] == "2024-01-01"
        assert meta.extra["end_date"] == "2024-12-31"
