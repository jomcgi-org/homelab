"""Pure matching layer that pairs Route B scan rows against Semgrep Managed
Scans (SMS) rows into side-by-side comparison rows.

Matching order (bucketed first by is_full_scan, since a full scan never pairs
with a PR scan): commit match, then ref match, then nearest-date match
(main full scans only), then any remainder is emitted one-sided.

Pure and deterministic: never touches a clock, never calls datetime.now(),
only compares the datetimes it is given.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

_SIDE_FIELDS = ("scan_id", "total_time", "findings_total", "scan_completed_at")


def _side(row: dict) -> dict:
    """Project a row dict down to the fields the output's route_b/sms side needs."""
    return {field: row[field] for field in _SIDE_FIELDS}


def _bucket(rows: list[dict]) -> dict[bool, list[dict]]:
    """Split rows into is_full_scan buckets, preserving input order within each."""
    buckets: dict[bool, list[dict]] = {True: [], False: []}
    for row in rows:
        buckets[bool(row["is_full_scan"])].append(row)
    return buckets


def _time_delta(a: Optional[datetime], b: Optional[datetime]) -> Optional[float]:
    """Absolute seconds between two possibly-None datetimes, or None if either is missing."""
    if a is None or b is None:
        return None
    return abs((a - b).total_seconds())


def _nearest(row_b: dict, candidates: list[dict]) -> Optional[dict]:
    """Pick the candidate closest in scan_completed_at to row_b. A candidate
    with a comparable (non-None on both sides) delta always beats one where
    either side's date is None; ties broken by input order (stable)."""
    best: Optional[dict] = None
    best_key: Optional[tuple] = None
    for cand in candidates:
        delta = _time_delta(
            row_b.get("scan_completed_at"), cand.get("scan_completed_at")
        )
        # sortable key: (has_no_comparable_delta, delta_or_0) -> False sorts before True
        key = (delta is None, delta if delta is not None else 0.0)
        if best_key is None or key < best_key:
            best_key = key
            best = cand
    return best


def _pair_by_key(
    route_b: list[dict], sms: list[dict], key_field: str
) -> tuple[list[tuple[dict, dict]], list[dict], list[dict]]:
    """Pair unmatched route_b/sms rows that share a non-empty value of
    key_field, choosing the closest sms candidate by scan_completed_at for
    each route_b row (each side used at most once). Returns
    (pairs, leftover_route_b, leftover_sms)."""
    remaining_sms = list(sms)
    pairs: list[tuple[dict, dict]] = []
    leftover_route_b: list[dict] = []

    for rb_row in route_b:
        key_val = rb_row.get(key_field) or ""
        if not key_val:
            leftover_route_b.append(rb_row)
            continue
        candidates = [s for s in remaining_sms if (s.get(key_field) or "") == key_val]
        if not candidates:
            leftover_route_b.append(rb_row)
            continue
        chosen = _nearest(rb_row, candidates)
        pairs.append((rb_row, chosen))
        remaining_sms.remove(chosen)

    return pairs, leftover_route_b, remaining_sms


def _date_pair(
    route_b: list[dict], sms: list[dict]
) -> tuple[list[tuple[dict, dict]], list[dict], list[dict]]:
    """Nearest-date pairing among is_full_scan branch=='main' rows, skipping
    any row whose scan_completed_at is None (those never get date-matched).
    Each side used at most once."""

    def _eligible(row: dict) -> bool:
        return row.get("branch") == "main" and row.get("scan_completed_at") is not None

    rb_eligible = [r for r in route_b if _eligible(r)]
    rb_ineligible = [r for r in route_b if not _eligible(r)]

    remaining_sms = [s for s in sms if _eligible(s)]
    sms_ineligible = [s for s in sms if not _eligible(s)]

    pairs: list[tuple[dict, dict]] = []
    leftover_route_b: list[dict] = []

    for rb_row in rb_eligible:
        if not remaining_sms:
            leftover_route_b.append(rb_row)
            continue
        chosen = _nearest(rb_row, remaining_sms)
        pairs.append((rb_row, chosen))
        remaining_sms.remove(chosen)

    leftover_route_b = leftover_route_b + rb_ineligible
    leftover_sms = remaining_sms + sms_ineligible

    return pairs, leftover_route_b, leftover_sms


def _speedup(rb_row: dict, sms_row: dict) -> Optional[float]:
    total_time = rb_row.get("total_time") or 0.0
    if total_time <= 0:
        return None
    return sms_row["total_time"] / total_time


def _make_row(rb_row: Optional[dict], sms_row: Optional[dict], match_kind: str) -> dict:
    source = rb_row if rb_row is not None else sms_row
    return {
        "commit_sha": source["commit_sha"],
        "scan_ref": source["scan_ref"],
        "is_full_scan": source["is_full_scan"],
        "branch": source["branch"],
        "route_b": _side(rb_row) if rb_row is not None else None,
        "sms": _side(sms_row) if sms_row is not None else None,
        "speedup": _speedup(rb_row, sms_row)
        if rb_row is not None and sms_row is not None
        else None,
        "match_kind": match_kind,
    }


def _best_date(row: dict) -> Optional[datetime]:
    """The row's best available scan_completed_at, preferring route_b."""
    date = None
    if row["route_b"] is not None:
        date = row["route_b"]["scan_completed_at"]
    if date is None and row["sms"] is not None:
        date = row["sms"]["scan_completed_at"]
    return date


def build_comparisons(route_b: list[dict], sms: list[dict]) -> list[dict]:
    """Pair Route B scans against SMS scans into side-by-side comparison rows.

    Matching, bucketed by is_full_scan (a full scan never pairs with a PR
    scan):
      1. commit: same non-empty commit_sha, closest scan_completed_at.
      2. ref: among still-unmatched, same non-empty scan_ref, closest
         scan_completed_at.
      3. date: among still-unmatched is_full_scan=True branch='main' rows,
         nearest-in-time pairing (rows with a None date are skipped here).
      4. one-sided: anything still unmatched becomes a one-sided row.

    Output rows are sorted by the best available scan_completed_at
    descending (dated rows first, newest first; None-dated rows last).
    """
    result: list[dict] = []

    rb_buckets = _bucket(route_b)
    sms_buckets = _bucket(sms)

    for is_full in (True, False):
        rb_rows = rb_buckets[is_full]
        sms_rows = sms_buckets[is_full]

        commit_pairs, rb_rows, sms_rows = _pair_by_key(rb_rows, sms_rows, "commit_sha")
        for rb_row, sms_row in commit_pairs:
            result.append(_make_row(rb_row, sms_row, "commit"))

        ref_pairs, rb_rows, sms_rows = _pair_by_key(rb_rows, sms_rows, "scan_ref")
        for rb_row, sms_row in ref_pairs:
            result.append(_make_row(rb_row, sms_row, "ref"))

        if is_full:
            date_pairs, rb_rows, sms_rows = _date_pair(rb_rows, sms_rows)
            for rb_row, sms_row in date_pairs:
                result.append(_make_row(rb_row, sms_row, "date"))

        for rb_row in rb_rows:
            result.append(_make_row(rb_row, None, "one-sided"))
        for sms_row in sms_rows:
            result.append(_make_row(None, sms_row, "one-sided"))

    # Dated rows sorted newest-first, then None-dated rows appended last
    # (stable, preserving their bucket/match-kind order). A single sort with
    # reverse=True can't express this because reversing also flips the
    # "has a date" flag ordering, which would put None-dated rows first.
    dated = [row for row in result if _best_date(row) is not None]
    undated = [row for row in result if _best_date(row) is None]
    dated.sort(key=_best_date, reverse=True)
    return dated + undated


def _median(values: list[float]) -> float | None:
    """Median of a non-empty list, or None if empty."""
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _percentile(values: list[float], q: float) -> float | None:
    """Linear-interpolated percentile (q in [0, 1]), or None if empty."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    lo = int(pos)
    frac = pos - lo
    if frac == 0:
        return ordered[lo]
    return ordered[lo] + (ordered[lo + 1] - ordered[lo]) * frac


def build_aggregates(comparisons: list[dict]) -> dict:
    """Summarise matched comparisons into per-type aggregates.

    Only two-sided rows (both homelab and managed ran) count: a one-sided row
    is not a comparison. Buckets by scan type ("pr" vs "full") since PR and
    full-scan runtimes differ by an order of magnitude and must not be pooled.
    ``speedup`` is managed_median / homelab_median (greater than 1 means
    homelab is faster). ``findings_pairs``/``findings_agree`` count pairs
    where both sides reported a findings total, and how many of those totals
    match exactly (a results-parity signal alongside the speed numbers).
    Returns a stable shape even for empty buckets so the page can render
    placeholders.
    """
    buckets: dict[str, list[dict]] = {"pr": [], "full": []}
    for row in comparisons:
        if row.get("route_b") and row.get("sms"):
            buckets["full" if row.get("is_full_scan") else "pr"].append(row)

    out: dict[str, dict] = {}
    for key, pairs in buckets.items():
        if not pairs:
            out[key] = {
                "pairs": 0,
                "homelab_median": None,
                "managed_median": None,
                "speedup": None,
                "findings_pairs": 0,
                "findings_agree": 0,
            }
            continue
        homelab_median = _median([p["route_b"]["total_time"] for p in pairs])
        managed_median = _median([p["sms"]["total_time"] for p in pairs])
        speedup = (
            managed_median / homelab_median
            if homelab_median and homelab_median > 0
            else None
        )
        findings_sides = [
            (p["route_b"]["findings_total"], p["sms"]["findings_total"])
            for p in pairs
            if p["route_b"]["findings_total"] is not None
            and p["sms"]["findings_total"] is not None
        ]
        out[key] = {
            "pairs": len(pairs),
            "homelab_median": homelab_median,
            "managed_median": managed_median,
            "speedup": speedup,
            "findings_pairs": len(findings_sides),
            "findings_agree": sum(1 for rb, sms in findings_sides if rb == sms),
        }
    return out


def build_distributions(route_b: list[dict], sms: list[dict]) -> dict:
    """Runtime distributions over ALL scans in the window, not just matched
    pairs. Matched-pair medians answer "same work, who was faster"; these
    answer "what does each side's runtime look like overall", which matters
    while managed coverage is sparse (only scans that left an open finding
    are visible via the Semgrep API, so most homelab scans have no pair).

    Shape: {"pr"|"full": {"homelab"|"managed": {n, p50, p90, min, max}}}.
    Rows without a total_time are excluded from n and the stats.
    """
    out: dict[str, dict] = {}
    for bucket_key, is_full in (("pr", False), ("full", True)):
        out[bucket_key] = {}
        for side_key, rows in (("homelab", route_b), ("managed", sms)):
            times = [
                r["total_time"]
                for r in rows
                if bool(r["is_full_scan"]) == is_full and r["total_time"] is not None
            ]
            out[bucket_key][side_key] = {
                "n": len(times),
                "p50": _percentile(times, 0.5),
                "p90": _percentile(times, 0.9),
                "min": min(times) if times else None,
                "max": max(times) if times else None,
            }
    return out
