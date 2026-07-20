"""Tests for the semgrep demo core: validation, queueing, savings math."""

import asyncio

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from ember_public import semgrep_core
from ember_public.models import DemoSgSavings  # noqa: F401  (registers the table)


def test_validate_rejects_oversize():
    err = semgrep_core.validate_snippet("python", "x = 1\n" * 300)
    assert err is not None and "lines" in err


def test_validate_rejects_long_chars():
    err = semgrep_core.validate_snippet("python", "x" * 20_000)
    assert err is not None and "characters" in err


def test_validate_rejects_bad_language():
    assert semgrep_core.validate_snippet("rust", "fn main() {}") is not None


def test_validate_accepts_small_python():
    assert semgrep_core.validate_snippet("python", "import os\n") is None


def test_snippet_path_by_language():
    assert semgrep_core.snippet_path("python") == "snippet.py"
    assert semgrep_core.snippet_path("javascript") == "snippet.js"


def test_scan_rate_bucket_blocks_rapid_repeat():
    tag = "tag-rate-test"
    assert semgrep_core.check_and_record_scan(tag) is True
    assert semgrep_core.check_and_record_scan(tag) is False


@pytest.mark.asyncio
async def test_queue_rejects_when_full():
    # Fill every slot and the whole waiting queue, then expect rejection.
    sem = semgrep_core._make_queue(slots=1, max_waiters=1)
    async with sem.slot():  # holds the only slot

        async def waiter():
            async with sem.slot():
                pass

        t = asyncio.create_task(waiter())
        await asyncio.sleep(0.01)
        # ...the next is bounced immediately
        with pytest.raises(semgrep_core.QueueFullError):
            async with sem.slot():
                pass
        t.cancel()


def test_savings_delta_uses_baseline():
    assert (
        semgrep_core.saved_ms(scan_ms=1000) == semgrep_core.HOSTED_SCAN_MEDIAN_MS - 1000
    )
    assert semgrep_core.saved_ms(scan_ms=999_999) == 0  # never negative


# ---------------------------------------------------------------------------
# demo_sg_savings accrual: "scan time saved versus a hosted single-file
# scan", credited directly from each successful scan's scan_ms, no polling
# and no state machine (mirrors bazel_query_savings' accrual test pattern).
# ---------------------------------------------------------------------------


@pytest.fixture()
def _savings_db():
    """In-memory SQLite with only demo_sg_savings created. SQLModel.metadata
    .create_all fails on sqlite once schema-qualified tables are registered
    in the shared metadata, so this is scoped with
    tables=[DemoSgSavings.__table__] (mirrors bazel_core_test.py's
    _savings_db fixture)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine, tables=[DemoSgSavings.__table__])
    with Session(engine) as session:
        yield session


def test_demo_sg_savings_first_credit_creates_row(_savings_db):
    totals = semgrep_core.record_demo_sg_savings_core(_savings_db, scan_ms=900)
    assert totals == {
        "scans": 1,
        "actual_ms": 900,
        "saved_ms": semgrep_core.HOSTED_SCAN_MEDIAN_MS - 900,
    }


def test_demo_sg_savings_accumulates_across_scans(_savings_db):
    semgrep_core.record_demo_sg_savings_core(_savings_db, scan_ms=900)
    totals = semgrep_core.record_demo_sg_savings_core(_savings_db, scan_ms=1100)
    assert totals == {
        "scans": 2,
        "actual_ms": 2000,
        "saved_ms": (semgrep_core.HOSTED_SCAN_MEDIAN_MS - 900)
        + (semgrep_core.HOSTED_SCAN_MEDIAN_MS - 1100),
    }


def test_demo_sg_savings_never_credits_negative_even_if_scan_ms_exceeds_baseline(
    _savings_db,
):
    huge_scan_ms = semgrep_core.HOSTED_SCAN_MEDIAN_MS + 5_000
    totals = semgrep_core.record_demo_sg_savings_core(_savings_db, scan_ms=huge_scan_ms)
    assert totals == {"scans": 1, "actual_ms": huge_scan_ms, "saved_ms": 0}
