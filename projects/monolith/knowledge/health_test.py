"""Unit coverage for knowledge extraction lane health."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from knowledge.extraction import EXTRACTION_VERSION
from knowledge.health import _kg_health_core, set_swept_last_cycle


class _Result:
    def __init__(self, value):
        self.value = value

    def one(self):
        return self.value

    def scalar_one(self):
        return self.value

    def one_or_none(self):
        return self.value


class _Session:
    def __init__(
        self,
        queue,
        provenance,
        disputes=None,
        repo_diff=None,
        quality=None,
        burst=None,
    ):
        self.results = iter(
            [
                _Result(queue),
                _Result(provenance),
                _Result(quality or SimpleNamespace(rejected_24h=0, corrected_24h=0)),
                _Result(4),
                _Result(
                    disputes
                    or SimpleNamespace(
                        open_disputes=0,
                        oldest_open_dispute_seconds=None,
                    )
                ),
                _Result(repo_diff or SimpleNamespace(last_sha=None, last_run_at=None)),
                _Result(burst),
            ]
        )
        self.calls = []

    def execute(self, statement, params):
        self.calls.append((str(statement), params))
        return next(self.results)


@pytest.mark.parametrize(
    ("oldest", "failed", "atoms", "expected_ok"),
    [
        (0, 1, 0, False),
        (0, 1, 1, True),
        (6 * 60 * 60 + 1, 0, 0, False),
    ],
)
def test_kg_health_marks_failures_without_atoms_or_stale_queue_unhealthy(
    oldest, failed, atoms, expected_ok
):
    session = _Session(
        SimpleNamespace(queued=2, oldest_seconds=oldest),
        SimpleNamespace(
            failed_24h=failed,
            atoms_24h=atoms,
            last_success_at=datetime(2026, 9, 3, 12, 0, 0),
        ),
    )

    result = _kg_health_core(session, 40)

    assert result["ok"] is expected_ok
    assert result["last_success_at"] == "2026-09-03T12:00:00+00:00"


def test_kg_health_filters_lane_version_and_counts_null_success_rows():
    session = _Session(
        SimpleNamespace(queued=0, oldest_seconds=None),
        SimpleNamespace(failed_24h=0, atoms_24h=0, last_success_at=None),
    )

    _kg_health_core(session, 40)

    provenance_sql, provenance_params = session.calls[1]
    assert "derived_note_id IS DISTINCT FROM 'failed'" in provenance_sql
    assert provenance_params == {"version": EXTRACTION_VERSION}


def test_kg_health_reports_rejected_and_corrected_counts():
    session = _Session(
        SimpleNamespace(queued=0, oldest_seconds=None),
        SimpleNamespace(failed_24h=0, atoms_24h=0, last_success_at=None),
        quality=SimpleNamespace(rejected_24h=7, corrected_24h=3),
    )

    result = _kg_health_core(session, 40)

    assert result["rejected_24h"] == 7
    assert result["corrected_24h"] == 3
    quality_sql, quality_params = session.calls[2]
    assert "extraction_rejected" in quality_sql
    assert "extraction_passes" in quality_sql
    assert quality_params == {"version": EXTRACTION_VERSION}


def test_kg_health_reports_stale_open_disputes_and_last_sweep():
    set_swept_last_cycle(7)
    session = _Session(
        SimpleNamespace(queued=0, oldest_seconds=None),
        SimpleNamespace(failed_24h=0, atoms_24h=0, last_success_at=None),
        SimpleNamespace(
            open_disputes=2,
            oldest_open_dispute_seconds=48 * 60 * 60 + 1,
        ),
        SimpleNamespace(
            last_sha="a" * 40,
            last_run_at=datetime(2026, 9, 3, 13, 0, 0),
        ),
    )

    result = _kg_health_core(session, 40)

    assert result["ok"] is False
    assert result["open_disputes"] == 2
    assert result["oldest_open_dispute_seconds"] == 48 * 60 * 60 + 1
    assert result["swept_last_cycle"] == 7
    assert result["repo_diff_last_sha"] == "a" * 40
    assert result["repo_diff_last_run_at"] == "2026-09-03T13:00:00+00:00"


def test_kg_health_reports_active_burst_and_remaining_allowance():
    now = datetime.now(timezone.utc)
    session = _Session(
        SimpleNamespace(queued=0, oldest_seconds=None),
        SimpleNamespace(failed_24h=0, atoms_24h=0, last_success_at=None),
        burst=SimpleNamespace(
            extra_jobs=1_000,
            used_jobs=125,
            created_at=now - timedelta(hours=1),
            expires_at=now + timedelta(hours=2),
            created_by="standing:operator@example.com",
        ),
    )

    result = _kg_health_core(session, 150)

    assert result["effective_cap"] == 1_025
    assert result["burst"]["active"] is True
    assert result["burst"]["remaining_jobs"] == 875
    assert result["burst"]["expires_at"] is not None
