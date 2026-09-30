from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from sync import (
    DEFAULT_SPEC_DIR,
    HoneycombClient,
    SpecError,
    diff_trigger,
    execute,
    load_specs,
    main,
    normalize_query,
    parse_spec,
    plan,
    render,
)

RECIPIENT = "kjEJr2qxxxe"

RAW = {
    "name": "EmberVM session-create denials sustained",
    "dataset": "embervm-control",
    "description": "Counts   denials.\n  Fires on two evaluations.",
    "enabled": True,
    "frequency": 300,
    "alert_type": "on_change",
    "threshold": {"op": ">", "value": 10, "exceeded_limit": 2},
    "recipients": [RECIPIENT],
    "tags": {"service": "embervm"},
    "query": {
        "time_range": 900,
        "calculations": [{"op": "COUNT"}],
        "filters": [
            {"column": "name", "op": "=", "value": "embervm.session.create"},
            {"column": "ember.reason", "op": "exists"},
        ],
        "breakdowns": ["ember.placement.outcome"],
    },
}


def spec(**overrides):
    raw = copy.deepcopy(RAW)
    raw.update(overrides)
    return parse_spec(raw)


def live_from(s, trigger_id="t1", **overrides):
    """A live trigger as Honeycomb echoes it back for spec ``s``."""
    trigger = {
        "id": trigger_id,
        "name": s.name,
        "description": s.description,
        "disabled": not s.enabled,
        "frequency": s.frequency,
        "alert_type": s.alert_type,
        "threshold": dict(s.threshold),
        "recipients": [
            {"id": r, "type": "webhook", "target": "Discord"} for r in s.recipients
        ],
        "tags": [{"key": k, "value": v} for k, v in s.tags.items()],
        "query_id": "q1",
    }
    trigger.update(overrides)
    return trigger


def echoed_query(s):
    """Honeycomb's echo: defaults filled in, nulls, filters reordered."""
    q = copy.deepcopy(s.query)
    q["filters"] = list(reversed(q["filters"]))
    for f in q["filters"]:
        f.setdefault("value", None)
    q["calculations"] = [dict(c, column=None) for c in q["calculations"]]
    q.update(filter_combination="AND", havings=[], orders=None, calculated_fields=[])
    return q


# -- spec validation ------------------------------------------------------


def test_description_whitespace_is_folded():
    assert spec().description == "Counts denials. Fires on two evaluations."


def test_exceeded_limit_defaults_to_one():
    raw = copy.deepcopy(RAW)
    del raw["threshold"]["exceeded_limit"]
    assert parse_spec(raw).threshold["exceeded_limit"] == 1


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"description": "x" * 1024}, "limit 1023"),
        ({"frequency": 90}, "multiple of 60"),
        ({"alert_type": "sometimes"}, "alert_type"),
        ({"threshold": {"op": "==", "value": 1}}, "threshold.op"),
        ({"threshold": {"op": ">", "value": 1, "exceeded_limit": 6}}, "1-5"),
        ({"recipients": []}, "recipient"),
        ({"tags": {str(i): "v" for i in range(11)}}, "10 tags"),
        ({"surprise": 1}, "unknown keys"),
    ],
)
def test_invalid_specs_are_rejected(overrides, message):
    with pytest.raises(SpecError, match=message):
        spec(**overrides)


@pytest.mark.parametrize(
    "query_overrides, message",
    [
        ({"time_range": 60}, "between frequency"),
        ({"time_range": 1500}, "between frequency"),  # > 4 x 300
        ({"orders": [{"op": "COUNT"}]}, "cannot use"),
        ({"limit": 10}, "cannot use"),
        (
            {"calculations": [{"op": "COUNT"}, {"op": "MAX", "column": "x"}]},
            "exactly 1",
        ),
        (
            {"filters": [{"column": "root.name", "op": "=", "value": "x"}]},
            "relational",
        ),
    ],
)
def test_invalid_queries_are_rejected(query_overrides, message):
    raw = copy.deepcopy(RAW)
    raw["query"].update(query_overrides)
    with pytest.raises(SpecError, match=message):
        parse_spec(raw)


def test_checked_in_specs_are_valid_and_uniquely_named():
    specs = load_specs(DEFAULT_SPEC_DIR)
    assert specs, "no trigger specs found"
    for s in specs:
        assert s.recipients == (RECIPIENT,), s.name


def test_imported_trigger_is_kept():
    names = {s.name for s in load_specs(DEFAULT_SPEC_DIR)}
    assert "jomcgi.dev /health composite unhealthy" in names


def test_log_triggers_stay_disabled_until_logs_land():
    for s in load_specs(DEFAULT_SPEC_DIR):
        if s.dataset == "k8s-logs":
            assert not s.enabled, s.name


def test_duplicate_spec_names_are_rejected(tmp_path: Path):
    for name in ("a.yaml", "b.yaml"):
        (tmp_path / name).write_text(json.dumps(RAW))
    with pytest.raises(SpecError, match="duplicate"):
        load_specs(tmp_path)


# -- diffing --------------------------------------------------------------


def test_echoed_trigger_is_in_sync():
    s = spec()
    assert diff_trigger(s, live_from(s), echoed_query(s)) == {}


def test_filter_order_is_not_drift_but_breakdown_order_is():
    a = {"breakdowns": ["x", "y"], "filters": [{"column": "a"}, {"column": "b"}]}
    b = {"breakdowns": ["x", "y"], "filters": [{"column": "b"}, {"column": "a"}]}
    assert normalize_query(a) == normalize_query(b)
    b["breakdowns"] = ["y", "x"]
    assert normalize_query(a) != normalize_query(b)


def test_missing_exceeded_limit_on_live_means_one():
    s = spec(threshold={"op": ">", "value": 10})
    live = live_from(s, threshold={"op": ">", "value": 10})
    assert diff_trigger(s, live, echoed_query(s)) == {}


@pytest.mark.parametrize(
    "overrides, key",
    [
        ({"threshold": {"op": ">", "value": 20, "exceeded_limit": 2}}, "threshold"),
        ({"disabled": True}, "disabled"),
        ({"frequency": 600}, "frequency"),
        ({"description": "hand edited"}, "description"),
        ({"recipients": [{"id": "someone-else"}]}, "recipients"),
        ({"tags": []}, "tags"),
    ],
)
def test_trigger_field_drift_is_reported(overrides, key):
    s = spec()
    changes = diff_trigger(s, live_from(s, **overrides), echoed_query(s))
    assert set(changes) == {key}


def test_query_drift_is_reported():
    s = spec()
    q = echoed_query(s)
    q["time_range"] = 1800
    changes = diff_trigger(s, live_from(s), q)
    assert set(changes) == {"query"}
    live, want = changes["query"]
    assert live["time_range"] == 1800 and want["time_range"] == 900


# -- planning -------------------------------------------------------------


def test_plan_creates_missing_updates_drifted_and_leaves_matching():
    missing, drifted, same = (
        spec(name="missing"),
        spec(name="drifted"),
        spec(name="same"),
    )
    live = [
        (
            "embervm-control",
            live_from(drifted, "d1", frequency=900),
            echoed_query(drifted),
        ),
        ("embervm-control", live_from(same, "s1"), echoed_query(same)),
    ]
    actions = {
        a.name: a for a in plan([missing, drifted, same], live, {"embervm-control"})
    }
    assert actions["missing"].kind == "create"
    assert actions["drifted"].kind == "update"
    assert actions["drifted"].trigger_id == "d1"
    assert set(actions["drifted"].changes) == {"frequency"}
    assert actions["same"].kind == "noop"


def test_plan_reports_unmanaged_triggers_without_touching_them():
    s = spec()
    stray = {"id": "x9", "name": "made by hand"}
    actions = plan([s], [("embervm-control", stray, {})], {"embervm-control"})
    kinds = {a.name: a.kind for a in actions}
    assert kinds == {s.name: "create", "made by hand": "unmanaged"}


def test_plan_skips_specs_whose_dataset_does_not_exist():
    s = spec(dataset="k8s-logs", enabled=False)
    [action] = plan([s], [], set())
    assert action.kind == "skip"
    assert "does not exist" in action.reason


def test_plan_refuses_duplicate_live_names():
    s = spec()
    live = [
        ("embervm-control", live_from(s, "a"), echoed_query(s)),
        ("embervm-control", live_from(s, "b"), echoed_query(s)),
    ]
    with pytest.raises(SpecError, match="share this name"):
        plan([s], live, {"embervm-control"})


def test_plan_refuses_dataset_moves():
    s = spec()
    live = [("metrics", live_from(s), echoed_query(s))]
    with pytest.raises(SpecError, match="cannot move"):
        plan([s], live, {"metrics", "embervm-control"})


def test_second_plan_after_apply_is_a_noop():
    s = spec()
    [first] = plan([s], [], {"embervm-control"})
    assert first.kind == "create"
    body = s.body("q1")
    created = dict(body, id="t1")
    [second] = plan(
        [s], [("embervm-control", created, echoed_query(s))], {"embervm-control"}
    )
    assert second.kind == "noop"


def test_render_shows_live_and_spec_values():
    s = spec()
    [action] = plan(
        [s],
        [("embervm-control", live_from(s, frequency=900), echoed_query(s))],
        {"embervm-control"},
    )
    out = render([action])
    assert "UPDATE" in out and "live: 900" in out and "spec: 300" in out


# -- execution and entry point -------------------------------------------


class FakeClient(HoneycombClient):
    def __init__(self):
        self.calls = []

    def create_query(self, dataset, query):
        self.calls.append(("query", dataset, query))
        return "q-new"

    def create_trigger(self, dataset, body):
        self.calls.append(("create", dataset, body))
        return {"id": "t-new"}

    def update_trigger(self, dataset, trigger_id, body):
        self.calls.append(("update", dataset, trigger_id, body))
        return {}


def test_execute_only_writes_creates_and_updates():
    s = spec()
    other = spec(name="other")
    live = [
        ("embervm-control", live_from(other, "o1", frequency=900), echoed_query(other))
    ]
    actions = plan([s, other], live, {"embervm-control"})
    client = FakeClient()
    execute(client, actions)
    kinds = [c[0] for c in client.calls]
    assert kinds == ["query", "create", "query", "update"]
    create_body = client.calls[1][2]
    assert create_body["query_id"] == "q-new"
    assert create_body["recipients"] == [{"id": RECIPIENT}]
    assert create_body["disabled"] is False
    assert client.calls[3][2] == "o1"


def test_main_requires_the_config_key(monkeypatch, capsys):
    monkeypatch.delenv("HONEYCOMB_CONFIG_KEY", raising=False)
    assert main([]) == 2
    assert "HONEYCOMB_CONFIG_KEY" in capsys.readouterr().err


def test_fetch_live_skips_missing_datasets_and_fetches_unechoed_queries():
    import io
    import urllib.error

    from sync import fetch_live

    s = spec()
    trigger = live_from(s)  # no inline "query", only query_id
    routes = {
        "/1/datasets/embervm-control": {"slug": "embervm-control"},
        "/1/triggers/embervm-control": [trigger],
        "/1/queries/embervm-control/q1": echoed_query(s),
    }
    seen = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def opener(req, timeout):
        path = req.full_url.removeprefix("https://api.honeycomb.io")
        seen.append((req.get_method(), path, req.get_header("X-honeycomb-team")))
        if path not in routes:
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(b"{}"))
        return Resp(json.dumps(routes[path]).encode())

    client = HoneycombClient("key", opener=opener)
    live, existing = fetch_live(client, ["embervm-control", "k8s-logs"])
    assert existing == {"embervm-control"}
    assert [(d, t["id"]) for d, t, _ in live] == [("embervm-control", "t1")]
    assert diff_trigger(s, live[0][1], live[0][2]) == {}
    assert all(key == "key" for _, _, key in seen)
    assert ("GET", "/1/triggers/k8s-logs", "key") not in seen
