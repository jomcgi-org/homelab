"""Read-only, bounded candidate shaping for the existing problem-issue producer.

No provider calls, GitHub writes, reservations, or scheduler registration. Inputs
are reviewed snapshots, not authorizations to spend. See the adjacent README.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class PreviewPolicy:
    max_age_seconds: int = 900
    horizon_seconds: int = 302400  # half a week
    minimum_remaining_percent: float = 50
    minimum_surplus_percent: float = 10
    minimum_short_remaining_percent: float = 10
    max_findings: int = 20
    max_drafts: int = 2
    cooldown_seconds: int = 86400
    max_unproven_attempts: int = 3

    def __post_init__(self):
        bounds = {
            "max_age_seconds": (1, 900),
            "horizon_seconds": (1, 302400),
            "max_findings": (1, 20),
            "max_drafts": (1, 2),
            "cooldown_seconds": (3600, 604800),
            "max_unproven_attempts": (1, 3),
        }
        for key, (low, high) in bounds.items():
            value = getattr(self, key)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"invalid {key}")
        for key in ("minimum_remaining_percent", "minimum_surplus_percent"):
            if not _number(getattr(self, key), 1, 100):
                raise ValueError(f"invalid {key}")
        if not _number(self.minimum_short_remaining_percent, 10, 100):
            raise ValueError("invalid minimum_short_remaining_percent")


def _number(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def _stamp(value):
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
        return result if result.tzinfo is not None else None
    except ValueError:
        return None


def _fresh(value, now, policy):
    stamp = _stamp(value)
    return (
        stamp is not None
        and 0 <= (now - stamp).total_seconds() <= policy.max_age_seconds
    )


def quota_decisions(
    payload: dict, *, now: datetime, policy: PreviewPolicy
) -> list[dict]:
    """Per-account decisions from broker headers; never combine provider totals."""
    if (
        not isinstance(payload, dict)
        or payload.get("available") is not True
        or payload.get("grants_complete") is not True
        or not _fresh(payload.get("fetched_at"), now, policy)
        or not isinstance(payload.get("grants"), dict)
        or len(payload["grants"]) > 64
    ):
        return [{"reason": "quota_inventory_unknown", "eligible": False}]
    decisions = []
    for grant, view in sorted(payload["grants"].items()):
        result = {"grant": grant, "eligible": False, "reason": "quota_unknown"}
        decisions.append(result)
        if not isinstance(view, dict):
            continue
        result["provider"] = view.get("provider")
        if (
            view.get("provider") not in ("codex", "claude")
            or view.get("grant") != grant
            or view.get("observed") is not True
        ):
            continue
        if not _fresh(view.get("observed_at"), now, policy):
            result["reason"] = "quota_stale_or_unknown"
            continue
        if view.get("status") != "allowed" or view.get("exhausted") is not False:
            result["reason"] = "quota_blocked"
            continue
        pacing = view.get("pacing")
        if pacing is not None:
            if not isinstance(pacing, dict) or pacing.get("state") not in (
                "on_pace",
                "use_more",
            ):
                result["reason"] = "broker_pacing_not_permissive"
                result["pacing_state"] = (
                    pacing.get("state") if isinstance(pacing, dict) else None
                )
                continue
        windows = view.get("windows")
        if not isinstance(windows, list) or not 1 <= len(windows) <= 16:
            continue
        checked = []
        observed = _stamp(view["observed_at"])
        for window in windows:
            if not isinstance(window, dict):
                break
            used, minutes = window.get("used_percent"), window.get("window_minutes")
            reset = _stamp(window.get("resets_at"))
            # Broker windows originate in provider headers. Explicit estimates
            # and unknown provenance from imported snapshots are never promoted.
            if (
                not _number(used, 0, 100)
                or type(minutes) is not int
                or not 1 <= minutes <= 525600
                or reset is None
                or window.get("reset_source", "provider_header") != "provider_header"
                or window.get("expired") is not False
                or not 0 < (reset - now).total_seconds()
                or (reset - observed).total_seconds() > minutes * 60 + 1
            ):
                break
            if used == 100:
                result["reason"] = "quota_exhausted"
                break
            checked.append((minutes, used, reset))
        if len(checked) != len(windows):
            continue
        # Conflicting durations are ambiguous even if window names differ.
        if len({minutes for minutes, _, _ in checked}) != len(checked):
            continue
        minutes, used, reset = max(checked)
        constrained = next(
            (
                (short_minutes, short_used, short_reset)
                for short_minutes, short_used, short_reset in checked
                if short_minutes < minutes
                and (
                    100 - short_used < policy.minimum_short_remaining_percent
                    or 100
                    - short_used
                    - 100 * (short_reset - now).total_seconds() / (short_minutes * 60)
                    < -5
                )
            ),
            None,
        )
        if constrained is not None:
            short_minutes, short_used, short_reset = constrained
            result.update(
                reason="short_window_conserve",
                constrained_window_minutes=short_minutes,
                short_remaining_percent=100 - short_used,
                seconds_until_short_reset=(short_reset - now).total_seconds(),
            )
            continue
        seconds = (reset - now).total_seconds()
        remaining = 100 - used
        surplus = remaining - 100 * seconds / (minutes * 60)
        eligible = (
            seconds <= policy.horizon_seconds
            and remaining >= policy.minimum_remaining_percent
            and surplus >= policy.minimum_surplus_percent
        )
        result.update(
            eligible=eligible,
            reason="underused" if eligible else "not_due",
            remaining_percent=remaining,
            seconds_until_reset=seconds,
            surplus_percent=surplus,
            resets_at=reset.isoformat(),
            reset_source="provider_header",
            window_minutes=minutes,
        )
    return decisions


def fingerprint(repo: str, key: str) -> str:
    """Stable problem identity, independent of grant, reset, title and evidence age."""
    return hashlib.sha256(f"{repo}\n{key}".encode()).hexdigest()


def preview(
    snapshot: dict, *, now: datetime, policy: PreviewPolicy | None = None
) -> dict:
    """Shape reviewable tickets only after an eligible intake sweep is empty.

    The caller supplies the intake's eligible issue numbers, a complete dedup
    inventory (issues, PRs and owners), policy/cap attestations and audit history.
    Missing or stale snapshots abstain. This function does not persist history.
    """
    policy = policy or PreviewPolicy()
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    result = {
        "mode": "preview_only",
        "reason": "context_unknown",
        "drafts": [],
        "skipped": [],
    }
    if not isinstance(snapshot, dict) or not _fresh(
        snapshot.get("captured_at"), now, policy
    ):
        return result
    if any(
        snapshot.get(key) is not True
        for key in (
            "intake_complete",
            "dedup_complete",
            "history_complete",
            "rules_reviewed",
        )
    ):
        return result
    repo = snapshot.get("repo")
    if repo != "jomcgi-org/homelab":
        return result
    ready = snapshot.get("ready_issue_numbers")
    if not isinstance(ready, list):
        return result
    if ready:
        return {
            **result,
            "reason": "existing_ready_work",
            "ready_issue_numbers": ready[:20],
        }
    decisions = quota_decisions(snapshot.get("quota", {}), now=now, policy=policy)
    result["quota_decisions"] = decisions
    # A reviewed cap attestation is per grant. It is not inferred from a model
    # price or from a positive quota balance. Preview cannot reserve that cap.
    caps = snapshot.get("subscription_caps", {})
    eligible = [
        d
        for d in decisions
        if d["eligible"]
        and isinstance(caps, dict)
        and isinstance(caps.get(d["grant"]), dict)
        and caps[d["grant"]].get("subscription_only") is True
        and caps[d["grant"]].get("overage_disabled") is True
        and caps[d["grant"]].get("evidence")
    ]
    if not eligible:
        return {**result, "reason": "no_verified_spare_subscription"}
    history = snapshot.get("history")
    inventory = snapshot.get("inventory")
    findings = snapshot.get("findings")
    goals = snapshot.get("active_goal_ids")
    if not all(isinstance(v, list) for v in (history, inventory, findings, goals)):
        return result
    if any(type(goal) is not int or goal <= 0 for goal in goals):
        return result
    if len(history) > 1000 or len(inventory) > 1000:
        return {**result, "reason": "context_limit"}
    attempts = []
    for row in history:
        if not isinstance(row, dict) or _stamp(row.get("attempted_at")) is None:
            return result
        if _stamp(row["attempted_at"]) > now:
            return result
        attempts.append(row)
    attempts.sort(key=lambda row: _stamp(row["attempted_at"]), reverse=True)
    if (
        attempts
        and (now - _stamp(attempts[0]["attempted_at"])).total_seconds()
        < policy.cooldown_seconds
    ):
        return {**result, "reason": "cooldown"}
    unproven = 0
    for row in attempts:
        if (
            row.get("outcome") == "useful_completion"
            and row.get("merged_pr")
            and row.get("acceptance_evidence")
        ):
            break
        unproven += 1
    if unproven >= policy.max_unproven_attempts:
        return {**result, "reason": "usefulness_review_required"}
    occupied = set()
    for row in inventory:
        if not isinstance(row, dict) or not isinstance(row.get("problem_key"), str):
            return result
        occupied.add(fingerprint(repo, row["problem_key"]))
    occupied.update(
        row["fingerprint"]
        for row in attempts
        if isinstance(row.get("fingerprint"), str)
    )
    for finding in findings[: policy.max_findings]:
        required = (
            "problem_key",
            "title",
            "evidence",
            "reproduction",
            "benefit",
            "acceptance_tests",
            "owner",
        )
        if not isinstance(finding, dict) or any(
            not isinstance(finding.get(k), str)
            or not finding[k].strip()
            or len(finding[k]) > 4000
            for k in required
        ):
            result["skipped"].append("missing_evidence")
            continue
        key = fingerprint(repo, finding["problem_key"])
        if key in occupied:
            result["skipped"].append("duplicate_or_owned")
            continue
        if (
            finding.get("kind")
            not in (
                "bug",
                "failing_test",
                "performance",
                "reliability",
                "goal_improvement",
            )
            or type(finding.get("goal_id")) is not int
            or finding.get("goal_id") not in goals
            or finding.get("ready") is not True
            or not _fresh(finding.get("observed_at"), now, policy)
        ):
            result["skipped"].append("not_ready_or_goal_aligned")
            continue
        body = [
            f"<!-- factory-problem:{key} -->",
            "",
            "Quota improvement preview. Requires ordinary issue refinement before admission.",
        ]
        for heading, field in (
            ("Evidence", "evidence"),
            ("Reproduction", "reproduction"),
            ("Expected benefit", "benefit"),
            ("Acceptance tests", "acceptance_tests"),
            ("Proposed owner", "owner"),
        ):
            body.extend(("", f"## {heading}", "", finding[field]))
        body.extend(
            (
                "",
                f"Goal: {finding['goal_id']}",
                f"Problem key: {finding['problem_key']}",
                "Readiness: scoped for refinement; no execution or spending authorized.",
            )
        )
        result["drafts"].append(
            {"title": finding["title"], "body": "\n".join(body), "fingerprint": key}
        )
        occupied.add(key)
        if len(result["drafts"]) == policy.max_drafts:
            break
    result.update(
        reason="candidates" if result["drafts"] else "no_actionable_findings",
        findings_truncated=len(findings) > policy.max_findings,
    )
    return result


def main():
    """Read one bounded JSON snapshot from stdin and print a dry-run decision."""
    import sys

    raw = sys.stdin.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError("snapshot exceeds 1 MB")
    print(json.dumps(preview(json.loads(raw), now=datetime.now(UTC)), indent=2))


if __name__ == "__main__":
    main()
