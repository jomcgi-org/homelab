"""Tests for the semgrep demo core: validation, queueing, savings math."""

import asyncio

import pytest

from ember_public import semgrep_core


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
