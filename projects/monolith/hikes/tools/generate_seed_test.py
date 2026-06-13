"""Tests for generate_seed: SQL literal formatting and seed SQL generation.

Covers sql_literal() for all column types, generate() end-to-end against
an in-memory SQLite fixture, NULL handling, and BATCH_SIZE batching.
"""
import io
import sqlite3
import tempfile
from pathlib import Path

import pytest

from generate_seed import BATCH_SIZE, COLUMNS, generate, sql_literal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(rows):
    """Write a temporary SQLite file with a walks table and given rows."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    path = Path(tmp.name)
    conn = sqlite3.connect(str(path))
    conn.execute(
        """CREATE TABLE walks (
            uuid      TEXT,
            name      TEXT,
            url       TEXT,
            distance_km REAL,
            ascent_m  REAL,
            duration_h REAL,
            summary   TEXT,
            latitude  REAL,
            longitude REAL
        )"""
    )
    conn.executemany("INSERT INTO walks VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return path


def _row(
    uuid="uuid-1",
    name="Walk A",
    url="http://example.com/a",
    distance_km=5.0,
    ascent_m=200,
    duration_h=2.5,
    summary="A nice walk",
    latitude=57.0,
    longitude=-4.0,
):
    return (uuid, name, url, distance_km, ascent_m, duration_h, summary, latitude, longitude)


# ---------------------------------------------------------------------------
# sql_literal: text columns
# ---------------------------------------------------------------------------


class TestSqlLiteralTextColumns:
    def test_name_wrapped_in_single_quotes(self):
        assert sql_literal("name", "Ben Nevis") == "'Ben Nevis'"

    def test_single_quote_in_value_escaped(self):
        assert sql_literal("name", "Beinn a' Chlair") == "'Beinn a'' Chlair'"

    def test_multiple_single_quotes_all_escaped(self):
        assert sql_literal("summary", "It's Joe's walk") == "'It''s Joe''s walk'"

    def test_uuid_is_text_column(self):
        assert sql_literal("uuid", "abc-123-xyz") == "'abc-123-xyz'"

    def test_url_is_text_column(self):
        result = sql_literal("url", "https://www.walkhighlands.co.uk/ben-nevis")
        assert result == "'https://www.walkhighlands.co.uk/ben-nevis'"

    def test_summary_is_text_column(self):
        assert sql_literal("summary", "A great ridge walk") == "'A great ridge walk'"

    def test_empty_string_renders_as_two_single_quotes(self):
        assert sql_literal("summary", "") == "''"


# ---------------------------------------------------------------------------
# sql_literal: int columns
# ---------------------------------------------------------------------------


class TestSqlLiteralIntColumn:
    def test_integer_value_stays_integer(self):
        assert sql_literal("ascent_m", 500) == "500"

    def test_float_coerced_to_int(self):
        # 123.9 → int(123.9) = 123
        assert sql_literal("ascent_m", 123.9) == "123"

    def test_zero_ascent(self):
        assert sql_literal("ascent_m", 0) == "0"


# ---------------------------------------------------------------------------
# sql_literal: float columns
# ---------------------------------------------------------------------------


class TestSqlLiteralFloatColumns:
    def test_distance_km_uses_repr(self):
        assert sql_literal("distance_km", 12.5) == repr(12.5)

    def test_latitude_uses_repr(self):
        assert sql_literal("latitude", 57.1234) == repr(57.1234)

    def test_longitude_uses_repr(self):
        assert sql_literal("longitude", -4.567) == repr(-4.567)

    def test_duration_h_uses_repr(self):
        assert sql_literal("duration_h", 3.0) == repr(3.0)

    def test_integer_value_cast_to_float(self):
        # Passing an int for a float column: int(1) → float(1) → repr
        assert sql_literal("distance_km", 10) == repr(10.0)


# ---------------------------------------------------------------------------
# generate(): end-to-end
# ---------------------------------------------------------------------------


class TestGenerate:
    def test_single_row_emitted(self):
        """generate() reports (1, 0) for a single complete row."""
        db = _make_db([_row()])
        out = io.StringIO()
        emitted, skipped = generate(db, out)
        assert emitted == 1
        assert skipped == 0

    def test_output_contains_insert_statement(self):
        """Output includes INSERT INTO hikes.walks with all columns."""
        db = _make_db([_row(uuid="abc")])
        out = io.StringIO()
        generate(db, out)
        sql = out.getvalue()
        assert "INSERT INTO hikes.walks" in sql
        assert f"({', '.join(COLUMNS)})" in sql

    def test_output_contains_on_conflict(self):
        """Every INSERT batch ends with ON CONFLICT (uuid) DO NOTHING."""
        db = _make_db([_row()])
        out = io.StringIO()
        generate(db, out)
        assert "ON CONFLICT (uuid) DO NOTHING" in out.getvalue()

    def test_output_starts_with_header(self):
        """generate() always writes the header comment block first."""
        db = _make_db([_row()])
        out = io.StringIO()
        generate(db, out)
        assert out.getvalue().startswith("-- hikes.walks seed data")

    def test_uuid_value_in_output(self):
        """The row's uuid appears as a quoted SQL literal in the output."""
        db = _make_db([_row(uuid="my-unique-id")])
        out = io.StringIO()
        generate(db, out)
        assert "'my-unique-id'" in out.getvalue()

    def test_name_with_apostrophe_escaped_in_output(self):
        """Apostrophes in the walk name are doubled in the emitted SQL."""
        db = _make_db([_row(name="Beinn a' Chlair")])
        out = io.StringIO()
        generate(db, out)
        assert "Beinn a'' Chlair" in out.getvalue()

    def test_null_summary_coerced_not_skipped(self):
        """A row with NULL summary is emitted with summary='' and NOT skipped."""
        row = _row(summary=None)
        db = _make_db([row])
        out = io.StringIO()
        emitted, skipped = generate(db, out)
        assert emitted == 1
        assert skipped == 0

    def test_null_summary_becomes_empty_string_literal(self):
        """The coerced empty summary renders as '' in the output."""
        db = _make_db([_row(uuid="x", summary=None)])
        out = io.StringIO()
        generate(db, out)
        assert "''" in out.getvalue()

    def test_null_non_summary_column_causes_skip(self):
        """A row with NULL in a required (non-summary) column is skipped."""
        # NULL name column
        row = ("uuid-skip", None, "http://example.com", 3.0, 100, 1.5, "ok", 57.0, -4.0)
        db = _make_db([row])
        out = io.StringIO()
        emitted, skipped = generate(db, out)
        assert emitted == 0
        assert skipped == 1

    def test_null_uuid_skipped(self):
        """A row with NULL uuid is skipped."""
        row = (None, "Walk", "http://example.com", 3.0, 100, 1.5, "ok", 57.0, -4.0)
        db = _make_db([row])
        out = io.StringIO()
        emitted, skipped = generate(db, out)
        assert emitted == 0
        assert skipped == 1

    def test_empty_db_produces_only_header(self):
        """An empty walks table produces the header and zero INSERT statements."""
        db = _make_db([])
        out = io.StringIO()
        emitted, skipped = generate(db, out)
        sql = out.getvalue()
        assert emitted == 0
        assert skipped == 0
        assert "INSERT" not in sql
        assert sql.startswith("-- hikes.walks seed data")

    def test_rows_ordered_by_uuid(self):
        """Output rows are sorted by uuid so regeneration is a no-op diff."""
        rows = [
            _row(uuid="zzz-last", name="Last"),
            _row(uuid="aaa-first", name="First"),
        ]
        db = _make_db(rows)
        out = io.StringIO()
        generate(db, out)
        sql = out.getvalue()
        assert sql.index("'aaa-first'") < sql.index("'zzz-last'")

    def test_batch_size_boundary_produces_two_inserts(self):
        """BATCH_SIZE + 1 rows results in exactly two INSERT statements."""
        rows = [
            _row(
                uuid=f"uuid-{i:04d}",
                name=f"Walk {i}",
                url=f"http://example.com/{i}",
                distance_km=float(i + 1),
                ascent_m=i * 10,
                duration_h=float(i + 1) / 2,
            )
            for i in range(BATCH_SIZE + 1)
        ]
        db = _make_db(rows)
        out = io.StringIO()
        emitted, skipped = generate(db, out)
        sql = out.getvalue()
        assert emitted == BATCH_SIZE + 1
        assert skipped == 0
        assert sql.count("INSERT INTO hikes.walks") == 2

    def test_exact_batch_size_produces_one_insert(self):
        """Exactly BATCH_SIZE rows results in a single INSERT statement."""
        rows = [
            _row(
                uuid=f"uuid-{i:04d}",
                name=f"Walk {i}",
                url=f"http://example.com/{i}",
                distance_km=float(i + 1),
                ascent_m=i * 10,
                duration_h=float(i + 1) / 2,
            )
            for i in range(BATCH_SIZE)
        ]
        db = _make_db(rows)
        out = io.StringIO()
        emitted, skipped = generate(db, out)
        sql = out.getvalue()
        assert emitted == BATCH_SIZE
        assert sql.count("INSERT INTO hikes.walks") == 1

    def test_mixed_valid_and_null_rows(self):
        """generate() counts both emitted and skipped independently."""
        rows = [
            _row(uuid="good-1"),
            # NULL name: skipped
            ("bad-1", None, "http://example.com", 1.0, 50, 1.0, "ok", 57.0, -4.0),
            _row(uuid="good-2"),
            # NULL summary: coerced, NOT skipped
            _row(uuid="good-3", summary=None),
        ]
        db = _make_db(rows)
        out = io.StringIO()
        emitted, skipped = generate(db, out)
        assert emitted == 3
        assert skipped == 1
