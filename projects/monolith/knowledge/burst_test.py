"""Unit coverage for bounded knowledge extraction burst grants."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from knowledge.burst import kg_effective_cap


class _Result:
    def __init__(self, row):
        self.row = row

    def one_or_none(self):
        return self.row


class _Session:
    def __init__(self, row):
        self.row = row

    def execute(self, _statement, _params):
        return _Result(self.row)


NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


def _grant_row(*, extra_jobs: int, used_jobs: int, expires_at: datetime):
    return SimpleNamespace(
        extra_jobs=extra_jobs,
        used_jobs=used_jobs,
        created_at=NOW - timedelta(hours=1),
        expires_at=expires_at,
        created_by="standing:operator@example.com",
    )


def test_effective_cap_without_grant_equals_base_cap_exactly():
    assert kg_effective_cap(_Session(None), 150, now=NOW) == 150


@pytest.mark.parametrize("used_jobs", [0, 125, 999])
def test_active_grant_adds_its_full_size_regardless_of_use(used_jobs):
    # jobs_today already counts the used jobs, so subtracting them here too
    # halved every burst (#5778).
    row = _grant_row(
        extra_jobs=1_000,
        used_jobs=used_jobs,
        expires_at=NOW + timedelta(hours=2),
    )

    assert kg_effective_cap(_Session(row), 150, now=NOW) == 1_150


def test_expired_grant_contributes_zero_even_when_unused():
    row = _grant_row(
        extra_jobs=1_000,
        used_jobs=0,
        expires_at=NOW - timedelta(seconds=1),
    )

    assert kg_effective_cap(_Session(row), 150, now=NOW) == 150


@pytest.mark.parametrize("used_jobs", [1_000, 1_001])
def test_exhausted_grant_contributes_zero_before_expiry(used_jobs):
    row = _grant_row(
        extra_jobs=1_000,
        used_jobs=used_jobs,
        expires_at=NOW + timedelta(hours=2),
    )

    assert kg_effective_cap(_Session(row), 150, now=NOW) == 150
