#!/usr/bin/env python3
"""Reconcile Honeycomb triggers with the specs under ``triggers/``.

The spec files are the source of truth. Triggers are matched to specs by name,
so the sync is idempotent: a trigger that matches its spec is left alone, a
missing one is created, and one that drifted is updated. A live trigger with no
spec is reported as unmanaged and never touched, and nothing is ever deleted.

The default is a dry run that prints the plan. ``--apply`` executes it.
The Honeycomb configuration key is read from ``HONEYCOMB_CONFIG_KEY``.

The team is on Honeycomb's free plan, which allows exactly one trigger. The
plan refuses to go ahead when the specs plus the unmanaged live triggers
would add up to more than ``PLAN_TRIGGER_LIMIT`` (override with
``--plan-limit`` or ``HONEYCOMB_TRIGGER_LIMIT`` after a plan upgrade). Other
alert conditions live in the monolith; see the README.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

API_URL = "https://api.honeycomb.io"
# Honeycomb free plan: one trigger per team. Disabled triggers count too.
PLAN_TRIGGER_LIMIT = 1
PLAN_LIMIT_ENV = "HONEYCOMB_TRIGGER_LIMIT"
DEFAULT_SPEC_DIR = Path(__file__).resolve().parent / "triggers"

THRESHOLD_OPS = frozenset({">", ">=", "<", "<="})
ALERT_TYPES = frozenset({"on_change", "on_true", "on_group_change"})
# Query fields a trigger query may not carry (Honeycomb Triggers API).
FORBIDDEN_QUERY_FIELDS = frozenset(
    {"orders", "limit", "start_time", "end_time", "usage_mode"}
)
SPEC_KEYS = frozenset(
    {
        "name",
        "dataset",
        "description",
        "enabled",
        "frequency",
        "alert_type",
        "threshold",
        "recipients",
        "tags",
        "query",
    }
)


class SpecError(ValueError):
    """A trigger spec that Honeycomb would reject or that is ambiguous."""


class PlanLimitError(SpecError):
    """The plan would leave more triggers than the Honeycomb plan allows."""


def check_plan_limit(spec_count: int, unmanaged: Iterable[str], limit: int) -> None:
    """Refuse when specs plus unmanaged live triggers exceed the plan limit.

    Every spec becomes a live trigger once applied (disabled ones included,
    since Honeycomb counts them), and an unmanaged trigger keeps its slot
    because the sync never deletes.
    """
    unmanaged = sorted(unmanaged)
    total = spec_count + len(unmanaged)
    if total <= limit:
        return
    detail = f"{spec_count} spec(s)"
    if unmanaged:
        detail += f" + {len(unmanaged)} unmanaged live trigger(s) {unmanaged}"
    raise PlanLimitError(
        f"{detail} = {total} triggers, over the plan limit of {limit}. The "
        "Honeycomb free plan allows one trigger: alert in the monolith instead "
        "(projects/platform/honeycomb/README.md), or delete triggers in the UI."
    )


# --------------------------------------------------------------------------
# Specs
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TriggerSpec:
    name: str
    dataset: str
    description: str
    enabled: bool
    frequency: int
    alert_type: str
    threshold: dict
    recipients: tuple[str, ...]
    tags: dict
    query: dict
    source: str = ""

    def body(self, query_id: str) -> dict:
        """The trigger create/update payload."""
        return {
            "name": self.name,
            "description": self.description,
            "disabled": not self.enabled,
            "query_id": query_id,
            "frequency": self.frequency,
            "alert_type": self.alert_type,
            "threshold": dict(self.threshold),
            "evaluation_schedule_type": "frequency",
            "recipients": [{"id": r} for r in self.recipients],
            "tags": [{"key": k, "value": v} for k, v in sorted(self.tags.items())],
        }


def parse_spec(raw: dict, source: str = "") -> TriggerSpec:
    """Validate one spec against the Triggers API constraints."""
    where = source or raw.get("name", "<spec>")
    if not isinstance(raw, dict):
        raise SpecError(f"{where}: spec must be a mapping")
    unknown = set(raw) - SPEC_KEYS
    if unknown:
        raise SpecError(f"{where}: unknown keys {sorted(unknown)}")
    for key in ("name", "dataset", "description", "threshold", "query", "recipients"):
        if key not in raw:
            raise SpecError(f"{where}: missing {key!r}")

    name = str(raw["name"])
    if not 1 <= len(name) <= 120:
        raise SpecError(f"{where}: name must be 1-120 characters")
    description = " ".join(str(raw["description"]).split())
    if len(description) > 1023:
        raise SpecError(
            f"{where}: description is {len(description)} characters, limit 1023"
        )

    frequency = int(raw.get("frequency", 900))
    if not (60 <= frequency <= 86400 and frequency % 60 == 0):
        raise SpecError(f"{where}: frequency must be 60-86400 and a multiple of 60")

    alert_type = raw.get("alert_type", "on_change")
    if alert_type not in ALERT_TYPES:
        raise SpecError(f"{where}: alert_type must be one of {sorted(ALERT_TYPES)}")

    threshold = dict(raw["threshold"])
    if threshold.get("op") not in THRESHOLD_OPS:
        raise SpecError(f"{where}: threshold.op must be one of {sorted(THRESHOLD_OPS)}")
    if not isinstance(threshold.get("value"), (int, float)):
        raise SpecError(f"{where}: threshold.value must be a number")
    threshold.setdefault("exceeded_limit", 1)
    if not 1 <= int(threshold["exceeded_limit"]) <= 5:
        raise SpecError(f"{where}: threshold.exceeded_limit must be 1-5")

    recipients = tuple(str(r) for r in raw["recipients"])
    if not recipients:
        raise SpecError(f"{where}: at least one recipient is required")

    tags = {str(k): str(v) for k, v in (raw.get("tags") or {}).items()}
    if len(tags) > 10:
        raise SpecError(f"{where}: at most 10 tags")

    query = dict(raw["query"])
    _validate_query(query, frequency, where)

    return TriggerSpec(
        name=name,
        dataset=str(raw["dataset"]),
        description=description,
        enabled=bool(raw.get("enabled", True)),
        frequency=frequency,
        alert_type=alert_type,
        threshold=threshold,
        recipients=recipients,
        tags=tags,
        query=query,
        source=source,
    )


def _validate_query(query: dict, frequency: int, where: str) -> None:
    forbidden = FORBIDDEN_QUERY_FIELDS & set(query)
    if forbidden:
        raise SpecError(f"{where}: trigger queries cannot use {sorted(forbidden)}")
    time_range = query.get("time_range")
    if not isinstance(time_range, int):
        raise SpecError(f"{where}: query.time_range (seconds) is required")
    upper = min(4 * frequency, 86400)
    if not frequency <= time_range <= upper:
        raise SpecError(
            f"{where}: query.time_range must be between frequency ({frequency}) "
            f"and {upper}"
        )
    calculations = query.get("calculations") or []
    formulas = query.get("formulas") or []
    if formulas:
        if len(formulas) != 1:
            raise SpecError(f"{where}: a trigger query allows at most 1 formula")
    elif len(calculations) != 1:
        raise SpecError(f"{where}: a trigger query needs exactly 1 calculation")
    if len(query.get("havings") or []) > 1:
        raise SpecError(f"{where}: a trigger query allows at most 1 having")
    for f in (query.get("filters") or []) + (query.get("breakdowns") or []):
        column = f["column"] if isinstance(f, dict) else f
        if "." in column and column.split(".", 1)[0] in {
            "root",
            "parent",
            "child",
            "any",
            "any2",
            "any3",
            "none",
        }:
            raise SpecError(f"{where}: relational field {column!r} is not supported")


def load_specs(spec_dir: Path) -> list[TriggerSpec]:
    specs = []
    for path in sorted(spec_dir.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text())
        specs.append(parse_spec(raw, source=path.name))
    names = [s.name for s in specs]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise SpecError(f"duplicate trigger names in specs: {dupes}")
    return specs


# --------------------------------------------------------------------------
# Normalisation and diffing (pure; this is what the tests pin)
# --------------------------------------------------------------------------

_QUERY_DEFAULTS: dict[str, Any] = {
    "filter_combination": "AND",
    "breakdowns": [],
    "filters": [],
    "havings": [],
    "calculated_fields": [],
    "formulas": [],
}


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def normalize_query(query: dict | None) -> dict:
    """Reduce a query spec to the fields that change what a trigger evaluates.

    Honeycomb echoes queries back with defaults filled in, nulls for absent
    fields and its own list ordering; none of that is drift.
    """
    q = {k: v for k, v in (query or {}).items() if v not in (None, [], {})}
    for key, default in _QUERY_DEFAULTS.items():
        if q.get(key, default) == default:
            q.pop(key, None)
    for f in q.get("filters", []):
        if f.get("value") is None:
            f.pop("value", None)
    for c in q.get("calculations", []):
        for key in ("column", "name", "filters", "filter_combination"):
            if c.get(key) in (None, [], ""):
                c.pop(key, None)
    # Filter, having and calculated-field order does not change evaluation;
    # breakdown and calculation order is kept (it names the result columns).
    for key in ("filters", "havings", "calculated_fields"):
        if key in q:
            q[key] = sorted(q[key], key=_canonical)
    return q


def normalize_live(trigger: dict, query: dict | None) -> dict:
    threshold = dict(trigger.get("threshold") or {})
    threshold.setdefault("exceeded_limit", 1)
    return {
        "description": " ".join((trigger.get("description") or "").split()),
        "disabled": bool(trigger.get("disabled", False)),
        "frequency": trigger.get("frequency"),
        "alert_type": trigger.get("alert_type", "on_change"),
        "threshold": threshold,
        "recipients": sorted(r["id"] for r in trigger.get("recipients") or []),
        "tags": {t["key"]: t["value"] for t in trigger.get("tags") or []},
        "query": normalize_query(query),
    }


def normalize_spec(spec: TriggerSpec) -> dict:
    return {
        "description": spec.description,
        "disabled": not spec.enabled,
        "frequency": spec.frequency,
        "alert_type": spec.alert_type,
        "threshold": dict(spec.threshold),
        "recipients": sorted(spec.recipients),
        "tags": dict(spec.tags),
        "query": normalize_query(json.loads(json.dumps(spec.query))),
    }


def diff_trigger(spec: TriggerSpec, trigger: dict, query: dict | None) -> dict:
    """Field-level differences as {field: (live, desired)}; empty means in sync."""
    live = normalize_live(trigger, json.loads(json.dumps(query or {})))
    want = normalize_spec(spec)
    return {k: (live[k], want[k]) for k in want if live[k] != want[k]}


@dataclass
class Action:
    kind: str  # create | update | noop | skip | unmanaged
    name: str
    dataset: str
    spec: TriggerSpec | None = None
    trigger_id: str | None = None
    changes: dict = field(default_factory=dict)
    reason: str = ""


def plan(
    specs: Iterable[TriggerSpec],
    live: list[tuple[str, dict, dict | None]],
    existing_datasets: set[str],
    limit: int = PLAN_TRIGGER_LIMIT,
) -> list[Action]:
    """Decide what to do for every spec and every live trigger.

    ``live`` is (dataset_slug, trigger, query_spec) for each trigger found.
    Raises PlanLimitError when the result would exceed ``limit`` triggers.
    """
    specs = list(specs)
    by_name: dict[str, list[tuple[str, dict, dict | None]]] = {}
    for dataset, trigger, query in live:
        by_name.setdefault(trigger["name"], []).append((dataset, trigger, query))

    actions: list[Action] = []
    spec_names = set()
    for spec in specs:
        spec_names.add(spec.name)
        matches = by_name.get(spec.name, [])
        if len(matches) > 1:
            raise SpecError(
                f"{spec.name!r}: {len(matches)} live triggers share this name; "
                "rename or delete the extras in Honeycomb first"
            )
        if not matches:
            if spec.dataset not in existing_datasets:
                actions.append(
                    Action(
                        "skip",
                        spec.name,
                        spec.dataset,
                        spec=spec,
                        reason=f"dataset {spec.dataset!r} does not exist yet",
                    )
                )
            else:
                actions.append(Action("create", spec.name, spec.dataset, spec=spec))
            continue
        dataset, trigger, query = matches[0]
        if dataset != spec.dataset:
            raise SpecError(
                f"{spec.name!r}: live trigger is on dataset {dataset!r}, spec says "
                f"{spec.dataset!r}; Honeycomb cannot move a trigger between datasets"
            )
        changes = diff_trigger(spec, trigger, query)
        actions.append(
            Action(
                "update" if changes else "noop",
                spec.name,
                dataset,
                spec=spec,
                trigger_id=trigger["id"],
                changes=changes,
            )
        )
    for dataset, trigger, _ in live:
        if trigger["name"] not in spec_names:
            actions.append(
                Action("unmanaged", trigger["name"], dataset, trigger_id=trigger["id"])
            )
    check_plan_limit(
        len(specs), (a.name for a in actions if a.kind == "unmanaged"), limit
    )
    return actions


def render(actions: list[Action]) -> str:
    lines = []
    for a in actions:
        head = f"{a.kind.upper():<9} {a.dataset}: {a.name}"
        if a.trigger_id:
            head += f" [{a.trigger_id}]"
        if a.reason:
            head += f" ({a.reason})"
        lines.append(head)
        for key, (live, want) in sorted(a.changes.items()):
            lines.append(f"    {key}:")
            lines.append(f"      live: {_canonical(live)}")
            lines.append(f"      spec: {_canonical(want)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Honeycomb API
# --------------------------------------------------------------------------


class HoneycombClient:
    def __init__(
        self, key: str, base_url: str = API_URL, opener: Callable | None = None
    ):
        self._key = key
        self._base = base_url.rstrip("/")
        self._open = opener or urllib.request.urlopen

    def _call(self, method: str, path: str, body: dict | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self._base + path,
            data=data,
            method=method,
            headers={
                "X-Honeycomb-Team": self._key,
                "Content-Type": "application/json",
            },
        )
        try:
            with self._open(req, timeout=30) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")
            raise RuntimeError(f"{method} {path}: HTTP {err.code}: {detail}") from err
        return json.loads(payload) if payload else None

    def datasets(self) -> list[str]:
        return [dataset["slug"] for dataset in self._call("GET", "/1/datasets")]

    def dataset_exists(self, slug: str) -> bool:
        try:
            self._call("GET", f"/1/datasets/{slug}")
            return True
        except RuntimeError as err:
            if "HTTP 404" in str(err):
                return False
            raise

    def triggers(self, dataset: str) -> list[dict]:
        return self._call("GET", f"/1/triggers/{dataset}") or []

    def query(self, dataset: str, query_id: str) -> dict:
        return self._call("GET", f"/1/queries/{dataset}/{query_id}")

    def create_query(self, dataset: str, query: dict) -> str:
        return self._call("POST", f"/1/queries/{dataset}", query)["id"]

    def create_trigger(self, dataset: str, body: dict) -> dict:
        return self._call("POST", f"/1/triggers/{dataset}", body)

    def update_trigger(self, dataset: str, trigger_id: str, body: dict) -> dict:
        return self._call("PUT", f"/1/triggers/{dataset}/{trigger_id}", body)


def fetch_live(
    client: HoneycombClient, datasets: Iterable[str]
) -> tuple[list[tuple[str, dict, dict | None]], set[str]]:
    live, existing = [], set()
    for dataset in sorted(set(datasets)):
        if not client.dataset_exists(dataset):
            continue
        existing.add(dataset)
        for trigger in client.triggers(dataset):
            query = trigger.get("query")
            if query is None and trigger.get("query_id"):
                query = client.query(dataset, trigger["query_id"])
            live.append((dataset, trigger, query))
    return live, existing


def execute(client: HoneycombClient, actions: list[Action]) -> None:
    for a in actions:
        if a.kind not in {"create", "update"}:
            continue
        query_id = client.create_query(a.dataset, a.spec.query)
        body = a.spec.body(query_id)
        if a.kind == "create":
            created = client.create_trigger(a.dataset, body)
            print(f"created {a.name} [{created.get('id')}]")
        else:
            client.update_trigger(a.dataset, a.trigger_id, body)
            print(f"updated {a.name} [{a.trigger_id}]")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true", help="create and update triggers")
    p.add_argument("--spec-dir", type=Path, default=DEFAULT_SPEC_DIR)
    p.add_argument(
        "--plan-limit",
        type=int,
        default=None,
        help=f"max triggers the plan may leave (default {PLAN_TRIGGER_LIMIT}, "
        f"or ${PLAN_LIMIT_ENV})",
    )
    p.add_argument(
        "--dataset",
        action="append",
        default=[],
        help="extra dataset to scan for unmanaged triggers (repeatable)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    limit = args.plan_limit
    if limit is None:
        limit = int(os.environ.get(PLAN_LIMIT_ENV) or PLAN_TRIGGER_LIMIT)
    specs = load_specs(args.spec_dir)
    try:
        # Fail before touching the API: the specs alone may already be over.
        check_plan_limit(len(specs), (), limit)
    except PlanLimitError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    key = os.environ.get("HONEYCOMB_CONFIG_KEY", "")
    if not key:
        print("HONEYCOMB_CONFIG_KEY is not set", file=sys.stderr)
        return 2
    client = HoneycombClient(key)
    try:
        datasets = (
            {s.dataset for s in specs} | set(args.dataset) | set(client.datasets())
        )
        live, existing = fetch_live(client, datasets)
        actions = plan(specs, live, existing, limit)
    except (RuntimeError, SpecError) as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    print(render(actions))
    pending = [a for a in actions if a.kind in {"create", "update"}]
    if not args.apply:
        print(f"\ndry run: {len(pending)} change(s); rerun with --apply to execute")
        return 0
    try:
        execute(client, actions)
    except RuntimeError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
