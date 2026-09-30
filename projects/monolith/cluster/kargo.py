"""Plan a Kargo Promotion for ``kargo_promote``, from Kargo's own objects.

Pure: given a Stage, its project's Freight and a chart version, return the
Promotion to create or raise :class:`PromotionRefused` saying why not. The MCP
tool in ``cluster.mcp`` does the reads and the one write.

The tool is the lever for a Promotion that failed: Kargo never retries one, so
the Freight sits until something promotes it again (#6422). It is deliberately
narrower than the Kargo UI:

- It only promotes Freight the Stage can already take on its own: verified
  upstream and soaked there (per the Stage's ``availabilityStrategy`` and
  ``requiredSoakTime``), approved for the Stage by a person, or from a
  Warehouse the Stage subscribes to directly. It never approves Freight, so it
  cannot skip a gate a person has not waived.
- It refuses while any Promotion for the Stage is running or queued, rather
  than queueing behind it (a queued one for newer Freight would otherwise run
  first and leave the Stage on the older version).
- It refuses a version older than the Stage runs or last promoted unless the
  caller says ``rollback``: on a ``direct`` Stage every Freight the Warehouse
  ever found is available, so an agent working from a stale verdict could
  otherwise roll production back without meaning to.
- The steps and vars are the Stage's own ``promotionTemplate``, exactly what
  auto-promotion would have run.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from shared import rollout

API_VERSION = "kargo.akuity.io/v1alpha1"
_FINISHED = {"Succeeded", "Failed", "Errored", "Aborted"}

_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")
_DURATION_UNITS = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}


class PromotionRefused(Exception):
    """The Promotion would be wrong or rejected; the message says why."""


def _duration(value: Any) -> timedelta | None:
    """A Go duration string (``1h30m0s``) as a timedelta, or None if absent."""
    if not value:
        return None
    text = str(value)
    parts = _DURATION_PART.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        raise PromotionRefused(f"cannot read soak time {text!r}")
    return timedelta(seconds=sum(float(n) * _DURATION_UNITS[u] for n, u in parts))


def _timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _soaked(
    freight: dict[str, Any], upstream: str, required: timedelta, now: datetime
) -> bool:
    status = freight.get("status") or {}
    longest = _duration(
        ((status.get("verifiedIn") or {}).get(upstream) or {}).get("longestSoak")
    )
    since = _timestamp(
        ((status.get("currentlyIn") or {}).get(upstream) or {}).get("since")
    )
    current = now - since if since else None
    return max(t for t in (longest, current, timedelta(0)) if t is not None) >= required


def _origin_key(origin: dict[str, Any]) -> tuple[str, str]:
    return (str(origin.get("kind") or "Warehouse"), str(origin.get("name") or ""))


def _floor(
    stage: dict[str, Any],
    freights: list[dict[str, Any]],
    stage_name: str,
    chart: str | None,
) -> str | None:
    """The newest version the Stage runs or last promoted successfully."""
    candidates = [
        rollout.freight_version(f, chart)
        for f in freights
        if stage_name in ((f.get("status") or {}).get("currentlyIn") or {})
    ]
    last = (stage.get("status") or {}).get("lastPromotion") or {}
    if ((last.get("status") or {}).get("phase")) == "Succeeded":
        candidates.append(
            rollout.freight_version(
                last.get("freight") or (last.get("status") or {}).get("freight"), chart
            )
        )
    versions = [(parsed, v) for v in candidates if v and (parsed := rollout.semver(v))]
    return max(versions)[1] if versions else None


def _available(
    stage: dict[str, Any], freight: dict[str, Any], stage_name: str, now: datetime
) -> str | None:
    """None when Kargo would let the Stage take this Freight, else why not."""
    status = freight.get("status") or {}
    if stage_name in (status.get("approvedFor") or {}):
        return None
    verified = status.get("verifiedIn") or {}
    origin = freight.get("origin") or {}
    reasons = []
    for requested in (stage.get("spec") or {}).get("requestedFreight") or []:
        wanted = (requested or {}).get("origin") or {}
        if origin and wanted and _origin_key(origin) != _origin_key(wanted):
            # This entry requests a different Warehouse's Freight.
            continue
        sources = (requested or {}).get("sources") or {}
        if sources.get("direct"):
            return None
        upstream = [str(s) for s in sources.get("stages") or []]
        required = _duration(sources.get("requiredSoakTime")) or timedelta(0)
        ready = [
            s for s in upstream if s in verified and _soaked(freight, s, required, now)
        ]
        need_all = sources.get("availabilityStrategy") == "All"
        if upstream and (set(ready) == set(upstream) if need_all else ready):
            return None
        missing = [s for s in upstream if s not in ready]
        soak = f" and soaked for {sources['requiredSoakTime']}" if required else ""
        reasons.append(f"not verified{soak} in {', '.join(missing) or 'any upstream'}")
    detail = ", and ".join(reasons) or "the Stage requests no Freight"
    return (
        f"{detail}, and not approved for {stage_name}. Approving Freight past its "
        "gate is a person's decision, made in the Kargo UI"
    )


def plan_promotion(
    stage: dict[str, Any] | None,
    freights: list[dict[str, Any]],
    *,
    namespace: str,
    stage_name: str,
    chart: str | None,
    version: str,
    promotions: list[dict[str, Any]] = (),
    rollback: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The Promotion that re-runs ``stage_name`` for this chart version.

    ``promotions`` is the namespace's Promotions, used to refuse while one for
    this Stage is running or queued.
    """
    if not isinstance(stage, dict):
        raise PromotionRefused(f"stage {stage_name!r} not found in {namespace}")
    status = stage.get("status") or {}
    current = status.get("currentPromotion")
    unfinished = [
        (p.get("metadata") or {}).get("name")
        for p in promotions
        if isinstance(p, dict)
        and (p.get("spec") or {}).get("stage") == stage_name
        and (p.get("status") or {}).get("phase") not in _FINISHED
    ]
    if isinstance(current, dict) and current.get("name"):
        unfinished.insert(0, current["name"])
    if unfinished:
        raise PromotionRefused(
            f"Promotion {unfinished[0]} is still running or queued on {stage_name}. "
            "Wait for it to finish (verify_deployment shows its step)"
        )

    floor = _floor(stage, freights, stage_name, chart)
    want = rollout.semver(version)
    if not rollback and floor and want and want < rollout.semver(floor):
        raise PromotionRefused(
            f"{stage_name} already runs or last promoted {floor}, so promoting "
            f"{version} is a rollback. Pass rollback=True if that is intended"
        )

    matches = [
        f
        for f in freights
        if isinstance(f, dict) and rollout.freight_version(f, chart) == version
    ]
    if not matches:
        raise PromotionRefused(
            f"Kargo has no Freight for chart {chart} {version} in {namespace}"
        )
    freight = matches[0]
    name = (freight.get("metadata") or {}).get("name")
    if not name:
        raise PromotionRefused("the matching Freight has no name")

    why_not = _available(stage, freight, stage_name, now or datetime.now(timezone.utc))
    if why_not:
        label = freight.get("alias") or name
        raise PromotionRefused(f"Freight {label} ({version}) is {why_not}")

    template = ((stage.get("spec") or {}).get("promotionTemplate") or {}).get(
        "spec"
    ) or {}
    steps = template.get("steps")
    if not isinstance(steps, list) or not steps:
        raise PromotionRefused(f"stage {stage_name!r} has no promotion steps")
    spec: dict[str, Any] = {"stage": stage_name, "freight": name, "steps": steps}
    if template.get("vars"):
        spec["vars"] = template["vars"]
    return {
        "apiVersion": API_VERSION,
        "kind": "Promotion",
        # Kargo's webhook names the Promotion itself (stage.ulid.freight), but
        # the API server validates generateName first, and a trailing "." is
        # not a valid subdomain prefix. A trailing "-" is masked by that check.
        "metadata": {"generateName": f"{stage_name}-", "namespace": namespace},
        "spec": spec,
    }
