"""Private read endpoint for the Route B vs Semgrep Managed Scans (SMS)
performance comparison.

Loads scan_perf rows from Postgres, splits them by environment, and hands them
to the pure matcher in perf_compare.build_comparisons. Postgres read only, so
no ClusterRole/RBAC change is needed.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select

from app.db import get_session
from semgrep_scan.perf_compare import build_comparisons
from semgrep_scan.perf_store import ScanPerf

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
    }


@router.get("/perf")
async def get_perf(
    session: Session = Depends(get_session),
    limit: int = Query(300, ge=1, le=1000),
) -> dict:
    # NULLS LAST so any undated row never crowds out dated rows past the LIMIT
    # (route-b rows now carry scan_completed_at, but stay defensive for any
    # residual or legacy NULL-dated rows).
    rows = session.exec(
        select(ScanPerf)
        .order_by(ScanPerf.scan_completed_at.desc().nullslast())
        .limit(limit)
    ).all()

    route_b = [_row_to_dict(r) for r in rows if r.environment == "route-b"]
    sms = [_row_to_dict(r) for r in rows if r.environment == "managed-scans"]
    comparisons = build_comparisons(route_b, sms)

    return {
        "comparisons": comparisons,
        "coverage_note": (
            "Route B coverage is complete (captured at scan time). SMS coverage "
            "is best-effort: only Managed Scans that left an open finding are "
            "visible via the Semgrep API."
        ),
        "counts": {"route_b": len(route_b), "sms": len(sms)},
    }
