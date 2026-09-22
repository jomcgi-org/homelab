"""Tests for the advisory chart-lag and CI signals."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from cluster import cd_health as mod


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


@pytest.fixture(autouse=True)
def _clear_cache():
    mod._cache = None
    yield
    mod._cache = None


def _freight(version: str, created_at: datetime) -> dict:
    return {
        "name": f"freight-{version}",
        "version": version,
        "created_at": _iso(created_at),
    }


def _apps_config() -> str:
    """The deploy-shaped CD_HEALTH_KARGO_APPS payload: monolith + embervm."""
    return json.dumps(
        [
            {
                "app": "monolith",
                "kargo_namespace": "kargo-monolith",
                "chart_repo_suffix": "/charts/monolith",
                "chart_path": "projects/monolith/chart",
            },
            {
                "app": "embervm",
                "kargo_namespace": "kargo-embervm",
                "chart_repo_suffix": "/charts/embervm",
                "chart_path": "projects/embervm/chart",
            },
        ]
    )


# Per-app lag judgement (pure).
def test_up_to_date_is_ok():
    now = datetime.now(timezone.utc)
    fault, note = mod._evaluate_chart_lag(
        "monolith", "0.301.1", [_freight("0.301.0", now)], 7200, now
    )
    assert fault is None
    assert note == "monolith: prod on 0.301.1, up to date"


def test_empty_freight_faults_instead_of_saying_up_to_date():
    fault, note = mod._evaluate_chart_lag(
        "monolith", "0.301.1", [], 7200, datetime.now(timezone.utc)
    )
    assert fault is not None and "no Freight found" in fault
    assert "monolith" in fault
    assert note is None
    assert "up to date" not in fault


def test_behind_inside_window_does_not_fault_and_says_how_far():
    now = datetime.now(timezone.utc)
    fault, note = mod._evaluate_chart_lag(
        "monolith",
        "0.301.1",
        [_freight("0.302.0", now - timedelta(minutes=30))],
        7200,
        now,
    )
    assert fault is None
    assert note is not None and "0.302.0" in note and "30m" in note


def test_behind_past_window_faults_with_versions_and_age():
    now = datetime.now(timezone.utc)
    fault, note = mod._evaluate_chart_lag(
        "monolith",
        "0.301.1",
        [_freight("0.302.0", now - timedelta(hours=3))],
        7200,
        now,
    )
    assert note is None
    assert fault is not None
    assert "0.301.1" in fault and "0.302.0" in fault and "180m" in fault


def test_oldest_unpromoted_freight_owns_the_clock():
    now = datetime.now(timezone.utc)
    freight = [
        _freight("0.302.0", now - timedelta(hours=3)),
        _freight("0.303.0", now - timedelta(minutes=10)),
    ]
    fault, _ = mod._evaluate_chart_lag("monolith", "0.301.1", freight, 7200, now)
    assert fault is not None and "180m" in fault


def test_unreadable_live_version_reports_rather_than_faulting():
    fault, note = mod._evaluate_chart_lag(
        "monolith", "latest", [], 7200, datetime.now(timezone.utc)
    )
    assert fault is None
    assert note == "monolith: prod chart version unreadable"


def test_unparseable_freight_versions_are_ignored():
    now = datetime.now(timezone.utc)
    fault, note = mod._evaluate_chart_lag(
        "monolith", "0.301.1", [_freight("main", now - timedelta(days=2))], 7200, now
    )
    assert fault is None and note == "monolith: prod on 0.301.1, up to date"


def test_freight_older_than_live_is_ignored():
    now = datetime.now(timezone.utc)
    fault, note = mod._evaluate_chart_lag(
        "monolith", "0.301.1", [_freight("0.300.9", now - timedelta(days=2))], 7200, now
    )
    assert fault is None and note == "monolith: prod on 0.301.1, up to date"


# App-list config.
def test_parse_apps_accepts_the_deploy_shape():
    apps = mod._parse_apps(_apps_config())
    assert apps == [
        {
            "app": "monolith",
            "namespace": "kargo-monolith",
            "suffix": "/charts/monolith",
            "chart_path": "projects/monolith/chart",
        },
        {
            "app": "embervm",
            "namespace": "kargo-embervm",
            "suffix": "/charts/embervm",
            "chart_path": "projects/embervm/chart",
        },
    ]


def test_parse_apps_treats_unusable_config_as_empty():
    assert mod._parse_apps("") == []
    assert mod._parse_apps(None) == []
    assert mod._parse_apps("not json") == []
    # A JSON object is not the list the contract names.
    assert mod._parse_apps('{"app": "monolith"}') == []
    # An entry missing any of its three names is dropped, not half-honoured.
    assert mod._parse_apps('[{"app": "monolith"}]') == []
    assert mod._parse_apps('[{"app": "m", "kargo_namespace": "k"}]') == []
    assert mod._parse_apps('["monolith"]') == []


# Multi-app aggregation through _chart_lag_fault, with the Kubernetes client
# faked below the seam cd_health imports it at (cluster.kubernetes).
def _fake_kubernetes(revisions: dict, freight_by_namespace: dict):
    class _Fake:
        def __init__(self):
            self.freight_calls = []

        async def get_argocd_app_deployed_revision(self, name):
            return revisions.get(name)

        async def list_kargo_freight(self, namespace, repo_suffix="/charts/monolith"):
            self.freight_calls.append((namespace, repo_suffix))
            return freight_by_namespace.get(namespace, [])

    # cd_health constructs the client itself, so hand back an INSTANCE for the
    # patched KubernetesClient() to return.
    return _Fake()


@pytest.mark.asyncio
async def test_one_app_lagging_and_one_fresh_faults_once(monkeypatch):
    monkeypatch.setenv("CD_HEALTH_KARGO_APPS", _apps_config())
    now = datetime.now(timezone.utc)
    fake = _fake_kubernetes(
        {"monolith": "0.301.1", "embervm": "0.13.7"},
        {
            # monolith sits four hours behind; embervm has a fresh chart inside
            # the window, which is a note and never a fault.
            "kargo-monolith": [_freight("0.302.0", now - timedelta(hours=4))],
            "kargo-embervm": [_freight("0.14.0", now - timedelta(minutes=10))],
        },
    )
    monkeypatch.setattr("cluster.kubernetes.KubernetesClient", lambda: fake)

    ok, details = await mod._chart_lag_fault(7200)

    assert ok is False
    faults = [d for d in details if "waiting" in d]
    assert len(faults) == 1 and "monolith" in faults[0] and "0.302.0" in faults[0]
    # The fresh app contributes its note, not a fault, and names itself.
    assert any(d.startswith("embervm:") for d in details)
    # Each app must be queried against ITS namespace and chart suffix, proving
    # the config list actually flows through rather than a shared constant.
    assert fake.freight_calls == [
        ("kargo-monolith", "/charts/monolith"),
        ("kargo-embervm", "/charts/embervm"),
    ]


@pytest.mark.asyncio
async def test_one_app_lagging_one_fresh_exact_notes(monkeypatch):
    monkeypatch.setenv("CD_HEALTH_KARGO_APPS", _apps_config())
    now = datetime.now(timezone.utc)
    fake = _fake_kubernetes(
        {"monolith": "0.301.1", "embervm": "0.13.7"},
        {
            "kargo-monolith": [_freight("0.302.0", now - timedelta(hours=4))],
            "kargo-embervm": [_freight("0.14.0", now - timedelta(minutes=10))],
        },
    )
    monkeypatch.setattr("cluster.kubernetes.KubernetesClient", lambda: fake)

    ok, details = await mod._chart_lag_fault(7200)

    assert ok is False
    assert "embervm: prod 1 chart(s) behind through 0.14.0 for 10m" in details


@pytest.mark.asyncio
async def test_app_with_no_published_chart_yet_faults_others_stay_ok(monkeypatch):
    monkeypatch.setenv("CD_HEALTH_KARGO_APPS", _apps_config())
    now = datetime.now(timezone.utc)
    fake = _fake_kubernetes(
        {"monolith": "0.301.1", "embervm": "0.12.9"},
        {
            # embervm's Warehouse has discovered nothing yet: no published
            # chart for its suffix means the missing-data treatment, a fault.
            "kargo-monolith": [_freight("0.301.1", now)],
            "kargo-embervm": [],
        },
    )
    monkeypatch.setattr("cluster.kubernetes.KubernetesClient", lambda: fake)

    ok, details = await mod._chart_lag_fault(7200)

    assert ok is False
    faults = [d for d in details if "no Freight found" in d]
    assert len(faults) == 1 and "embervm" in faults[0]
    assert "monolith: prod on 0.301.1, up to date" in details


@pytest.mark.asyncio
async def test_empty_app_list_faults_instead_of_vacuous_health(monkeypatch):
    # Both spellings of "watches nothing": unset env and an explicit empty list.
    for raw in (None, "[]"):
        if raw is None:
            monkeypatch.delenv("CD_HEALTH_KARGO_APPS", raising=False)
        else:
            monkeypatch.setenv("CD_HEALTH_KARGO_APPS", raw)
        ok, details = await mod._chart_lag_fault(7200)
        assert ok is False
        assert any("no Kargo-managed apps configured" in d for d in details)
        assert not any("up to date" in d for d in details)


# CI signal. These encode the rules that took several passes to settle.
def test_quiet_green_main_is_healthy_at_any_age():
    week_old = datetime.now(timezone.utc) - timedelta(days=7)
    assert (
        mod._evaluate_ci(
            0,
            {"state": "success", "sha": "abc123456", "updated_at": _iso(week_old)},
            3600.0,
        )
        is None
    )


def test_red_but_recent_does_not_page():
    recent = datetime.now(timezone.utc) - timedelta(minutes=10)
    assert (
        mod._evaluate_ci(
            1,
            {"state": "failure", "sha": "abc123456", "updated_at": _iso(recent)},
            3600.0,
        )
        is None
    )


def test_red_past_the_window_pages():
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    fault = mod._evaluate_ci(
        0,
        {"state": "failure", "sha": "abc123456", "updated_at": _iso(old)},
        3600.0,
    )
    assert fault is not None and "abc123456" in fault


def test_commits_with_nothing_completed_pages_as_ci_down():
    fault = mod._evaluate_ci(3, None, 3600.0)
    assert fault is not None and "CI may be down" in fault


def test_no_commits_and_nothing_completed_is_ok():
    assert mod._evaluate_ci(0, None, 3600.0) is None


@pytest.mark.asyncio
async def test_chart_lag_check_error_reports_but_does_not_fault(monkeypatch):
    monkeypatch.delenv("GITHUB_API_TOKEN", raising=False)

    async def _boom(lag_s):
        raise RuntimeError("freights.kargo.akuity.io is forbidden")

    monkeypatch.setattr(mod, "_chart_lag_fault", _boom)
    result = await mod.cd_health()
    assert result["ok"] is True
    assert "chart lag check unavailable" in result["detail"]


@pytest.mark.asyncio
async def test_cd_health_reports_degraded_when_any_app_lags(monkeypatch):
    monkeypatch.delenv("GITHUB_API_TOKEN", raising=False)

    async def _lagging(lag_s):
        return False, [
            "embervm: prod on 0.12.9, 4 chart(s) behind through 0.13.3, waiting 240m"
        ]

    monkeypatch.setattr(mod, "_chart_lag_fault", _lagging)
    result = await mod.cd_health()
    assert result["ok"] is False
    assert "embervm" in result["detail"]


@pytest.mark.asyncio
async def test_result_is_cached(monkeypatch):
    monkeypatch.delenv("GITHUB_API_TOKEN", raising=False)
    calls = []

    async def _counting(lag_s):
        calls.append(1)
        return True, ["monolith: prod on 0.301.1, up to date"]

    monkeypatch.setattr(mod, "_chart_lag_fault", _counting)
    await mod.cd_health()
    await mod.cd_health()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_deployment_observations_are_default_off(monkeypatch):
    monkeypatch.delenv("GITHUB_API_TOKEN", raising=False)
    monkeypatch.delenv("DEPLOYMENT_OBSERVATIONS_ENABLED", raising=False)
    calls = []

    async def _healthy(lag_s):
        return True, []

    async def _record(poll_time):
        calls.append(poll_time)

    monkeypatch.setattr(mod, "_chart_lag_fault", _healthy)
    monkeypatch.setattr(mod, "_record_deployment_observations", _record)

    await mod.cd_health()

    assert calls == []


@pytest.mark.asyncio
async def test_only_cache_misses_record_deployment_observations(monkeypatch):
    monkeypatch.delenv("GITHUB_API_TOKEN", raising=False)
    monkeypatch.setenv("DEPLOYMENT_OBSERVATIONS_ENABLED", "true")
    polls = []

    async def _healthy(lag_s):
        return True, []

    async def _record(poll_time):
        polls.append(poll_time)

    monkeypatch.setattr(mod, "_chart_lag_fault", _healthy)
    monkeypatch.setattr(mod, "_record_deployment_observations", _record)

    await mod.cd_health()
    await mod.cd_health()

    assert len(polls) == 1


class _ObservationKubernetes:
    def __init__(self, app_results, freight_results):
        self.app_results = app_results
        self.freight_results = freight_results

    async def get_argocd_app_revisions(self, app):
        result = self.app_results[app]
        if isinstance(result, Exception):
            raise result
        return result

    async def list_kargo_freight(self, namespace, repo_suffix):
        result = self.freight_results[namespace]
        if isinstance(result, Exception):
            raise result
        return result


@pytest.mark.asyncio
async def test_collection_keeps_requested_deployed_and_freight_distinct(monkeypatch):
    poll_time = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    client = _ObservationKubernetes(
        {
            "monolith": {
                "requested_revision": "0.506.0",
                "deployed_revision": "0.505.1",
            }
        },
        {
            "kargo-monolith": [
                _freight("0.505.2", poll_time),
                _freight("0.507.0", poll_time),
            ]
        },
    )

    async def _resolve(chart_path, version):
        assert chart_path == "projects/monolith/chart"
        assert version == "0.507.0"
        return "a" * 40

    monkeypatch.setattr(mod, "_resolve_writeback_commit", _resolve)
    observation = await mod._collect_deployment_observation(
        client,
        mod._parse_apps(_apps_config())[0],
        poll_time,
        300,
    )

    assert observation["requested_revision"] == "0.506.0"
    assert observation["deployed_revision"] == "0.505.1"
    assert observation["newest_freight_version"] == "0.507.0"
    assert observation["writeback_commit"] == "a" * 40
    assert observation["status"] == "complete"
    assert observation["valid_until"] == "2026-09-20T12:12:30+00:00"


@pytest.mark.asyncio
async def test_writeback_resolution_requires_bot_author_path_and_version(monkeypatch):
    monkeypatch.setenv("GITHUB_API_TOKEN", "test-token")
    expected = "a" * 40

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {
                    "sha": "b" * 40,
                    "commit": {
                        "author": {"name": "someone-else"},
                        "message": "projects/monolith/chart: 0.506.0 -> 0.507.0",
                    },
                },
                {
                    "sha": "c" * 40,
                    "commit": {
                        "author": {"name": "chart-version-bot"},
                        "message": "projects/embervm/chart: 0.14.0 -> 0.507.0",
                    },
                },
                {
                    "sha": expected,
                    "commit": {
                        "author": {"name": "chart-version-bot"},
                        "message": (
                            "chore(charts): publish 1 chart version(s)\n\n"
                            "projects/monolith/chart: 0.506.0 -> 0.507.0"
                        ),
                    },
                },
            ]

    class _Http:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, params, headers):
            assert params["path"] == "projects/monolith/chart/Chart.yaml"
            assert params["sha"] == "main"
            return _Response()

    monkeypatch.setattr(mod.httpx, "AsyncClient", lambda **kwargs: _Http())

    assert (
        await mod._resolve_writeback_commit("projects/monolith/chart", "0.507.0")
        == expected
    )


@pytest.mark.asyncio
async def test_missing_provenance_is_not_an_upstream_error(monkeypatch):
    poll_time = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    entry = mod._parse_apps(_apps_config())[0]
    healthy = _ObservationKubernetes(
        {
            "monolith": {
                "requested_revision": "0.506.0",
                "deployed_revision": "0.505.1",
            }
        },
        {"kargo-monolith": [_freight("0.507.0", poll_time)]},
    )

    async def _missing(chart_path, version):
        return None

    monkeypatch.setattr(mod, "_resolve_writeback_commit", _missing)
    missing = await mod._collect_deployment_observation(healthy, entry, poll_time, 300)

    failing = _ObservationKubernetes(
        healthy.app_results,
        {"kargo-monolith": RuntimeError("Kargo unavailable")},
    )
    upstream = await mod._collect_deployment_observation(failing, entry, poll_time, 300)

    assert missing["status"] == "missing_provenance"
    assert missing["error_stage"] == "writeback_commit"
    assert "writeback_commit" not in missing
    assert upstream["status"] == "upstream_error"
    assert upstream["error_stage"] == "freight_read"
    assert "newest_freight_version" not in upstream


@pytest.mark.asyncio
async def test_per_app_write_failure_does_not_suppress_later_app(monkeypatch):
    monkeypatch.setenv("CD_HEALTH_KARGO_APPS", _apps_config())
    poll_time = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)
    fake = _ObservationKubernetes(
        {
            "monolith": RuntimeError("Application unavailable"),
            "embervm": {
                "requested_revision": "0.14.1",
                "deployed_revision": "0.14.0",
            },
        },
        {"kargo-embervm": [_freight("0.14.2", poll_time)]},
    )

    async def _missing(chart_path, version):
        return None

    writes = []

    async def _write(observation):
        if observation["app"] == "monolith":
            raise RuntimeError("database unavailable for first app")
        writes.append(observation)

    monkeypatch.setattr("cluster.kubernetes.KubernetesClient", lambda: fake)
    monkeypatch.setattr(mod, "_resolve_writeback_commit", _missing)
    monkeypatch.setattr(mod, "_write_deployment_observation", _write)

    await mod._record_deployment_observations(poll_time)

    assert [item["app"] for item in writes] == ["embervm"]
    assert writes[0]["observed_at"] == poll_time.isoformat()
