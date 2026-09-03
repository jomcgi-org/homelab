"""Unit coverage for knowledge extraction lane health."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from knowledge.extraction import EXTRACTION_VERSION
from knowledge.health import _kg_health_core


class _Result:
    def __init__(self, value):
        self.value = value

    def one(self):
        return self.value

    def scalar_one(self):
        return self.value


class _Session:
    def __init__(self, queue, provenance):
        self.results = iter([_Result(queue), _Result(provenance), _Result(4)])
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
