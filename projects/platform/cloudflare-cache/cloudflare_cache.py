"""Reconcile and monitor the public-host Cloudflare cache contract."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

_API_BASE = "https://api.cloudflare.com/client/v4"
_GITHUB_API_BASE = "https://api.github.com"
_POLICY_PATH = Path(__file__).with_name("policy.json")

_CACHE_MISS_QUERY = """
query CacheMissRate($zoneTag: string!, $hostname: string!, $start: Time!, $end: Time!) {
  viewer {
    zones(filter: {zoneTag: $zoneTag}) {
      total: httpRequestsAdaptiveGroups(
        limit: 1
        filter: {
          datetime_geq: $start
          datetime_lt: $end
          clientRequestHTTPHost: $hostname
        }
      ) { count }
      misses: httpRequestsAdaptiveGroups(
        limit: 1
        filter: {
          datetime_geq: $start
          datetime_lt: $end
          clientRequestHTTPHost: $hostname
          cacheStatus: "miss"
        }
      ) { count }
    }
  }
}
""".strip()


class APIError(RuntimeError):
    """An authenticated API returned a transport or application error."""


JSONResponse = dict[str, Any] | list[dict[str, Any]]
Transport = Callable[[str, str, dict[str, Any] | None], JSONResponse]


def _http_transport(base_url: str, token: str, *, github: bool = False) -> Transport:
    def request(
        method: str, path: str, body: dict[str, Any] | None = None
    ) -> JSONResponse:
        data = None if body is None else json.dumps(body).encode()
        headers = {
            "Accept": "application/vnd.github+json" if github else "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "homelab-cloudflare-cache/1",
        }
        req = Request(f"{base_url}{path}", data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=30) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise APIError(
                f"{method} {path} returned HTTP {exc.code}: {detail}"
            ) from exc
        except URLError as exc:
            raise APIError(f"{method} {path} failed: {exc.reason}") from exc
        if not raw:
            return {}
        parsed = json.loads(raw)
        if not isinstance(parsed, (dict, list)):
            raise APIError(f"{method} {path} returned an invalid JSON response")
        return parsed

    return request


class CloudflareAPI:
    def __init__(self, token: str, transport: Transport | None = None):
        self._transport = transport or _http_transport(_API_BASE, token)

    def rest(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        envelope = self._transport(method, path, body)
        if not isinstance(envelope, dict):
            raise APIError(
                f"Cloudflare API returned an invalid response for {method} {path}"
            )
        if envelope.get("success") is not True:
            raise APIError(
                f"Cloudflare API rejected {method} {path}: {envelope.get('errors')}"
            )
        return envelope.get("result")

    def graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        envelope = self._transport(
            "POST", "/graphql", {"query": query, "variables": variables}
        )
        if not isinstance(envelope, dict):
            raise APIError("Cloudflare GraphQL returned an invalid response")
        if envelope.get("errors"):
            raise APIError(
                f"Cloudflare GraphQL rejected the query: {envelope['errors']}"
            )
        data = envelope.get("data")
        if not isinstance(data, dict):
            raise APIError("Cloudflare GraphQL response did not contain data")
        return data


class GitHubAPI:
    def __init__(self, token: str, transport: Transport | None = None):
        self._transport = transport or _http_transport(
            _GITHUB_API_BASE, token, github=True
        )

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any] | list[dict[str, Any]]:
        result = self._transport(method, path, body)
        if not isinstance(result, (dict, list)):
            raise APIError(
                f"GitHub API returned an invalid response for {method} {path}"
            )
        return result


def load_policy(path: Path = _POLICY_PATH) -> dict[str, Any]:
    policy = json.loads(path.read_text())
    required = {
        "zone_name",
        "public_hostname",
        "zone_settings",
        "smart_tiered_cache",
        "cache_rule",
        "miss_alert",
    }
    if not isinstance(policy, dict) or set(policy) != required:
        raise ValueError(f"policy keys must be exactly {sorted(required)}")
    zone = policy["zone_name"]
    hostname = policy["public_hostname"]
    if not isinstance(zone, str) or not isinstance(hostname, str):
        raise TypeError("zone_name and public_hostname must be strings")
    if hostname == zone or not hostname.endswith(f".{zone}"):
        raise ValueError("public_hostname must be a subdomain of zone_name")
    if policy["zone_settings"] != {
        "browser_cache_ttl": 0,
        "always_online": "on",
    }:
        raise ValueError(
            "zone settings must respect origin headers and enable Always Online"
        )
    if policy["smart_tiered_cache"] != "on":
        raise ValueError("Smart Tiered Cache must be enabled")
    alert = policy["miss_alert"]
    if not 0 < alert["threshold_percent"] < 100:
        raise ValueError("miss alert threshold must be between 0 and 100")
    if alert["window_minutes"] < 10 or alert["minimum_requests"] < 1:
        raise ValueError(
            "miss alert needs a sustained window and a positive request floor"
        )
    return policy


def _zone_id(api: CloudflareAPI, zone_name: str) -> str:
    result = api.rest(
        "GET", f"/zones?{urlencode({'name': zone_name, 'status': 'active'})}"
    )
    if not isinstance(result, list):
        raise APIError("Cloudflare zone listing was not an array")
    matches = [zone for zone in result if zone.get("name") == zone_name]
    if len(matches) != 1 or not matches[0].get("id"):
        raise APIError(f"expected one active Cloudflare zone named {zone_name}")
    return str(matches[0]["id"])


def _canonical_expression(expression: str) -> str:
    expression = re.sub(r"\s+", " ", expression.strip())
    if expression.startswith("(") and expression.endswith(")"):
        expression = expression[1:-1].strip()
    return expression


def _rule_drift(ruleset: dict[str, Any], expected: dict[str, Any]) -> list[str]:
    wanted_expression = _canonical_expression(expected["expression"])
    candidates = [
        rule
        for rule in ruleset.get("rules", [])
        if rule.get("enabled", True)
        and _canonical_expression(str(rule.get("expression", ""))) == wanted_expression
    ]
    if len(candidates) != 1:
        return ["one enabled cache rule must match only the declared public hostname"]
    rule = candidates[0]
    drift: list[str] = []
    if rule.get("action") != expected["action"]:
        drift.append("public hostname rule action")
    actual_parameters = rule.get("action_parameters", {})
    for key, value in expected["action_parameters"].items():
        if actual_parameters.get(key) != value:
            drift.append(f"public hostname rule action_parameters.{key}")
    return drift


def inspect_settings(
    api: CloudflareAPI, policy: dict[str, Any]
) -> tuple[str, list[str]]:
    zone_id = _zone_id(api, policy["zone_name"])
    drift: list[str] = []
    for setting, expected in policy["zone_settings"].items():
        actual = api.rest("GET", f"/zones/{zone_id}/settings/{setting}")
        if actual.get("value") != expected:
            drift.append(f"zone setting {setting}")
    tiered = api.rest(
        "GET", f"/zones/{zone_id}/cache/tiered_cache_smart_topology_enable"
    )
    if tiered.get("value") != policy["smart_tiered_cache"]:
        drift.append("Smart Tiered Cache")
    ruleset = api.rest(
        "GET",
        f"/zones/{zone_id}/rulesets/phases/http_request_cache_settings/entrypoint",
    )
    drift.extend(_rule_drift(ruleset, policy["cache_rule"]))
    return zone_id, drift


def reconcile_settings(api: CloudflareAPI, policy: dict[str, Any]) -> list[str]:
    zone_id, drift = inspect_settings(api, policy)
    rule_drift = [
        item for item in drift if item.startswith(("public hostname", "one enabled"))
    ]
    if rule_drift:
        raise APIError(
            "refusing to change zone-wide settings until the public-only cache rule is safe: "
            + ", ".join(rule_drift)
        )
    changed: list[str] = []
    for setting, expected in policy["zone_settings"].items():
        label = f"zone setting {setting}"
        if label in drift:
            api.rest(
                "PATCH", f"/zones/{zone_id}/settings/{setting}", {"value": expected}
            )
            changed.append(label)
    if "Smart Tiered Cache" in drift:
        api.rest(
            "PATCH",
            f"/zones/{zone_id}/cache/tiered_cache_smart_topology_enable",
            {"value": policy["smart_tiered_cache"]},
        )
        changed.append("Smart Tiered Cache")
    return changed


def cache_miss_sample(
    api: CloudflareAPI,
    policy: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    alert = policy["miss_alert"]
    end = (now or datetime.now(timezone.utc)) - timedelta(
        minutes=alert["data_lag_minutes"]
    )
    start = end - timedelta(minutes=alert["window_minutes"])
    zone_id = _zone_id(api, policy["zone_name"])
    data = api.graphql(
        _CACHE_MISS_QUERY,
        {
            "zoneTag": zone_id,
            "hostname": policy["public_hostname"],
            "start": start.isoformat().replace("+00:00", "Z"),
            "end": end.isoformat().replace("+00:00", "Z"),
        },
    )
    zones = data.get("viewer", {}).get("zones", [])
    if len(zones) != 1:
        raise APIError("Cloudflare GraphQL did not return the requested zone")
    total = sum(int(group["count"]) for group in zones[0].get("total", []))
    misses = sum(int(group["count"]) for group in zones[0].get("misses", []))
    if misses > total:
        raise APIError("Cloudflare GraphQL returned more misses than total requests")
    rate = (100.0 * misses / total) if total else 0.0
    if total < alert["minimum_requests"]:
        state = "insufficient-data"
    elif rate > alert["threshold_percent"]:
        state = "firing"
    else:
        state = "healthy"
    return {
        "state": state,
        "hostname": policy["public_hostname"],
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "total_requests": total,
        "miss_requests": misses,
        "miss_rate_percent": round(rate, 4),
        "threshold_percent": alert["threshold_percent"],
        "minimum_requests": alert["minimum_requests"],
    }


def sync_alert_issue(github: GitHubAPI, repository: str, sample: dict[str, Any]) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("repository must have owner/name form")
    title = f"[alert] Cloudflare cache misses on {sample['hostname']}"
    issues: list[dict[str, Any]] = []
    for page in range(1, 11):
        batch = github.request(
            "GET",
            f"/repos/{repository}/issues?state=open&per_page=100&page={page}",
        )
        if not isinstance(batch, list):
            raise APIError("GitHub issue listing was not an array")
        issues.extend(batch)
        if len(batch) < 100:
            break
    else:
        raise APIError("more than 1,000 open issues prevents safe alert deduplication")
    matches = [
        issue
        for issue in issues
        if issue.get("title") == title and "pull_request" not in issue
    ]
    if len(matches) > 1:
        raise APIError(f"multiple open cache alert issues have title {title!r}")
    existing = matches[0] if matches else None
    if sample["state"] == "insufficient-data":
        return "unchanged-insufficient-data"
    if sample["state"] == "firing":
        if existing:
            return "already-open"
        body = (
            f"Cloudflare reported a {sample['miss_rate_percent']}% cache MISS rate "
            f"for `{sample['hostname']}` from {sample['start']} through {sample['end']} "
            f"({sample['miss_requests']} of {sample['total_requests']} requests).\n\n"
            f"The alert threshold is greater than {sample['threshold_percent']}% over "
            "the full sustained window. Desired state and the response runbook live in "
            "`projects/platform/cloudflare-cache/`."
        )
        created = github.request(
            "POST", f"/repos/{repository}/issues", {"title": title, "body": body}
        )
        if not isinstance(created, dict):
            raise APIError("GitHub issue creation did not return an object")
        return "opened"
    if existing:
        number = int(existing["number"])
        github.request(
            "POST",
            f"/repos/{repository}/issues/{number}/comments",
            {
                "body": (
                    f"Recovered: cache MISS rate is {sample['miss_rate_percent']}% "
                    f"for {sample['start']} through {sample['end']}."
                )
            },
        )
        github.request(
            "PATCH", f"/repos/{repository}/issues/{number}", {"state": "closed"}
        )
        return "closed"
    return "healthy"


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{name} is required")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("check", "apply", "monitor"))
    parser.add_argument("--policy", type=Path, default=_POLICY_PATH)
    args = parser.parse_args(argv)
    policy = load_policy(args.policy)
    cloudflare = CloudflareAPI(_required_env("CLOUDFLARE_API_TOKEN"))

    if args.command == "check":
        _, drift = inspect_settings(cloudflare, policy)
        print(json.dumps({"drift": drift}, sort_keys=True))
        return 1 if drift else 0
    if args.command == "apply":
        changed = reconcile_settings(cloudflare, policy)
        print(json.dumps({"changed": changed}, sort_keys=True))
        return 0

    sample = cache_miss_sample(cloudflare, policy)
    github = GitHubAPI(_required_env("GH_TOKEN"))
    action = sync_alert_issue(github, _required_env("GITHUB_REPOSITORY"), sample)
    print(json.dumps({"action": action, "sample": sample}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (APIError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
