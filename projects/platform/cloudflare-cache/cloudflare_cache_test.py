from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import cloudflare_cache
import pytest

POLICY = cloudflare_cache.load_policy(Path(__file__).with_name("policy.json"))


class FakeCloudflare:
    def __init__(self, *, settings=None, rule=None, graph=None):
        self.settings = settings or {
            "browser_cache_ttl": 0,
            "always_online": "on",
            "tiered": "on",
        }
        self.rule = rule or {
            "enabled": True,
            "expression": '(http.host wildcard r"public.jomcgi.dev")',
            "action": "set_cache_settings",
            "action_parameters": deepcopy(POLICY["cache_rule"]["action_parameters"]),
        }
        self.graph = graph or {"total": [{"count": 100}], "misses": [{"count": 5}]}
        self.writes = []

    def rest(self, method, path, body=None):
        if path.startswith("/zones?"):
            return [{"id": "zone-123", "name": "jomcgi.dev"}]
        if path.endswith("/rulesets/phases/http_request_cache_settings/entrypoint"):
            return {"rules": [self.rule]}
        if path.endswith("/cache/tiered_cache_smart_topology_enable"):
            if method == "PATCH":
                self.writes.append((path, body))
                self.settings["tiered"] = body["value"]
            return {"value": self.settings["tiered"]}
        setting = path.rsplit("/", 1)[-1]
        if method == "PATCH":
            self.writes.append((path, body))
            self.settings[setting] = body["value"]
        return {"value": self.settings[setting]}

    def graphql(self, query, variables):
        assert "httpRequestsAdaptiveGroups" in query
        assert variables["hostname"] == "public.jomcgi.dev"
        return {"viewer": {"zones": [self.graph]}}


class FakeGitHub:
    def __init__(self, issues=()):
        self.issues = list(issues)
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if method == "GET":
            return self.issues
        if method == "POST" and path.endswith("/issues"):
            return {"number": 99, **body}
        return {}


def _sample(total, misses):
    api = FakeCloudflare(
        graph={"total": [{"count": total}], "misses": [{"count": misses}]}
    )
    return cloudflare_cache.cache_miss_sample(
        api, POLICY, now=datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
    )


def test_policy_pins_public_hostname_and_safe_cache_rule():
    assert POLICY["public_hostname"] == "public.jomcgi.dev"
    assert POLICY["zone_settings"]["browser_cache_ttl"] == 0
    assert POLICY["cache_rule"]["action_parameters"] == {
        "cache": True,
        "edge_ttl": {"mode": "bypass_by_default"},
        "serve_stale": {"disable_stale_while_updating": False},
        "respect_strong_etags": True,
    }


def test_cloudflare_rest_rejects_a_non_success_response():
    api = cloudflare_cache.CloudflareAPI(
        "unused",
        transport=lambda _method, _path, _body: {
            "success": False,
            "errors": [{"message": "forbidden"}],
        },
    )

    with pytest.raises(cloudflare_cache.APIError, match="forbidden"):
        api.rest("GET", "/zones")


def test_cloudflare_graphql_rejects_query_errors():
    api = cloudflare_cache.CloudflareAPI(
        "unused",
        transport=lambda _method, _path, _body: {
            "errors": [{"message": "unsupported field"}],
        },
    )

    with pytest.raises(cloudflare_cache.APIError, match="unsupported field"):
        api.graphql("query Test { viewer { zones { zoneTag } } }", {})


def test_reconcile_is_noop_when_settings_match():
    api = FakeCloudflare()

    assert cloudflare_cache.reconcile_settings(api, POLICY) == []
    assert api.writes == []


def test_reconcile_updates_all_remaining_settings():
    api = FakeCloudflare(
        settings={
            "browser_cache_ttl": 432000,
            "always_online": "off",
            "tiered": "off",
        }
    )

    changed = cloudflare_cache.reconcile_settings(api, POLICY)

    assert changed == [
        "zone setting browser_cache_ttl",
        "zone setting always_online",
        "Smart Tiered Cache",
    ]
    assert [body for _, body in api.writes] == [
        {"value": 0},
        {"value": "on"},
        {"value": "on"},
    ]


@pytest.mark.parametrize(
    "rule",
    [
        {"enabled": True, "expression": 'http.host eq "jomcgi.dev"'},
        {
            "enabled": True,
            "expression": 'http.host wildcard r"public.jomcgi.dev"',
            "action": "set_cache_settings",
            "action_parameters": {
                "cache": True,
                "edge_ttl": {"mode": "override_origin", "default": 3600},
            },
        },
    ],
)
def test_reconcile_refuses_to_change_zone_settings_without_safe_public_rule(rule):
    api = FakeCloudflare(
        settings={
            "browser_cache_ttl": 432000,
            "always_online": "off",
            "tiered": "off",
        },
        rule=rule,
    )

    with pytest.raises(cloudflare_cache.APIError, match="refusing to change"):
        cloudflare_cache.reconcile_settings(api, POLICY)

    assert api.writes == []


def test_alert_fires_only_above_five_percent_for_the_full_window():
    assert _sample(100, 5)["state"] == "healthy"
    firing = _sample(100, 6)
    assert firing["state"] == "firing"
    assert firing["start"] == "2026-09-12T11:43:00Z"
    assert firing["end"] == "2026-09-12T11:58:00Z"


def test_alert_requires_enough_real_requests():
    sample = _sample(10, 10)
    assert sample["state"] == "insufficient-data"
    assert sample["miss_rate_percent"] == 100.0


def test_firing_sample_opens_one_github_issue():
    github = FakeGitHub()
    sample = _sample(100, 6)

    assert (
        cloudflare_cache.sync_alert_issue(github, "jomcgi-org/homelab", sample)
        == "opened"
    )
    create = github.calls[-1]
    assert create[0:2] == ("POST", "/repos/jomcgi-org/homelab/issues")
    assert "6.0% cache MISS rate" in create[2]["body"]


def test_insufficient_data_does_not_close_an_existing_alert():
    title = "[alert] Cloudflare cache misses on public.jomcgi.dev"
    github = FakeGitHub([{"number": 42, "title": title}])

    action = cloudflare_cache.sync_alert_issue(
        github, "jomcgi-org/homelab", _sample(10, 10)
    )

    assert action == "unchanged-insufficient-data"
    assert len(github.calls) == 1


def test_healthy_sample_comments_and_closes_existing_alert():
    title = "[alert] Cloudflare cache misses on public.jomcgi.dev"
    github = FakeGitHub([{"number": 42, "title": title}])

    action = cloudflare_cache.sync_alert_issue(
        github, "jomcgi-org/homelab", _sample(100, 5)
    )

    assert action == "closed"
    assert github.calls[-2][0:2] == (
        "POST",
        "/repos/jomcgi-org/homelab/issues/42/comments",
    )
    assert github.calls[-1] == (
        "PATCH",
        "/repos/jomcgi-org/homelab/issues/42",
        {"state": "closed"},
    )
