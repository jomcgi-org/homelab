"""Private read endpoint for the Route B vs Semgrep Managed Scans (SMS)
performance comparison.

Loads scan_perf rows from Postgres, splits them by environment, and hands them
to the pure matcher in perf_compare.build_comparisons. Postgres read only, so
no ClusterRole/RBAC change is needed.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func
from sqlmodel import Session, select

from core.db import get_session
from semgrep_scan.perf_compare import (
    build_aggregates,
    build_cohort_aggregates,
    build_comparisons,
    build_distributions,
    build_trend,
)
from semgrep_scan.perf_store import ScanPerf

_COVERAGE_NOTE = (
    "Comparisons start at the first homelab scan (earlier managed scans have no "
    "homelab counterpart and would be invalid cross-era comparisons, so they are "
    "excluded). Managed scan coverage is best-effort: only scans that left an "
    "open finding are visible via the Semgrep API."
)

router = APIRouter(prefix="/api/semgrep", tags=["semgrep-perf"])


def _row_to_dict(row: ScanPerf) -> dict:
    return {
        "scan_id": row.scan_id,
        "is_full_scan": row.is_full_scan,
        "branch": row.branch,
        "scan_ref": row.scan_ref,
        "commit_sha": row.commit_sha,
        "total_time": row.total_time,
        "findings_total": row.findings_total,
        "scan_completed_at": row.scan_completed_at,
        "file_count": row.file_count,
        "changed_lines": row.changed_lines,
        "languages": row.languages,
    }


def _empty_response() -> dict:
    return {
        "comparisons": [],
        "aggregates": build_aggregates([]),
        "cohorts": build_cohort_aggregates([]),
        "trend": build_trend([]),
        "distributions": build_distributions([], []),
        "window_start": None,
        "counts": {"homelab": 0, "managed": 0},
        "coverage_note": _COVERAGE_NOTE,
    }


@router.get("/perf")
async def get_perf(
    session: Session = Depends(get_session),
    limit: int = Query(300, ge=1, le=1000),
) -> dict:
    # Cutoff: the first homelab (route-b) scan. Every scan before it (all the
    # historical managed scans that predate homelab) is an invalid cross-era
    # comparison, so exclude both sides before that instant. With no homelab
    # scans yet, the comparison window has not opened: return an empty payload.
    window_start = session.exec(
        select(func.min(ScanPerf.scan_completed_at)).where(
            ScanPerf.environment == "route-b"
        )
    ).one()
    if window_start is None:
        return _empty_response()

    # NULLS LAST so any undated row never crowds out dated rows past the LIMIT.
    rows = session.exec(
        select(ScanPerf)
        .where(ScanPerf.scan_completed_at >= window_start)
        .order_by(ScanPerf.scan_completed_at.desc().nullslast())
        .limit(limit)
    ).all()

    homelab = [_row_to_dict(r) for r in rows if r.environment == "route-b"]
    managed = [_row_to_dict(r) for r in rows if r.environment == "managed-scans"]
    comparisons = build_comparisons(homelab, managed)

    return {
        "comparisons": comparisons,
        "aggregates": build_aggregates(comparisons),
        "cohorts": build_cohort_aggregates(comparisons),
        "trend": build_trend(comparisons),
        "distributions": build_distributions(homelab, managed),
        "window_start": window_start,
        "counts": {"homelab": len(homelab), "managed": len(managed)},
        "coverage_note": _COVERAGE_NOTE,
    }
