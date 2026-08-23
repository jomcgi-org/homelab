"""Reconcile Context Forge's authentik team_mapping from chart values (#5033).

Context Forge already reconciles MEMBERSHIP per request from the token's
`groups` claim (sso_service._apply_team_mapping), so authentik owns who is in
a team. What lived only in CF's Postgres, written by hand from a laptop, was
the WIRING: which team exists and which authentik group maps onto it. This
script makes Git own that half. Each run:

  1. lists teams and creates any declared team that is missing;
  2. reads the `authentik` sso_providers row and fails loudly if it is absent
     (creating it needs the OAuth client id, which stays with
     scripts/provision-mcp-auth.sh until #4569 settles);
  3. PUTs team_mapping (group name -> team id) back only when it differs,
     since a successful PUT invalidates CF's identity caches and a no-op
     write every tick would do that for nothing.

PUT /auth/sso/admin/providers/{id} drops None fields before applying, so
sending only team_mapping leaves client_id, issuer, api_audience and the rest
untouched.

Stdlib only: the gateway image bundles httpx today, but nothing here needs it
and urllib keeps the unit test free of the image.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Callable

# (method, path, body) -> (status, decoded json or None)
Api = Callable[[str, str, Any], tuple[int, Any]]


class ReconcileError(RuntimeError):
    """A condition the job must surface as a failed run, never paper over."""


def http_api(base_url: str, token: str, timeout: float = 30.0) -> Api:
    base = base_url.rstrip("/")

    def call(method: str, path: str, body: Any = None) -> tuple[int, Any]:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + token)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (in-cluster service URL from env)
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, raw.decode(errors="replace")

    return call


def parse_declared_teams(raw: str) -> list[dict[str, str]]:
    """Validate the values.yaml `teams` list rendered into TEAM_MAPPING_JSON."""
    teams = json.loads(raw)
    if not isinstance(teams, list) or not teams:
        raise ReconcileError("TEAM_MAPPING_JSON must be a non-empty list of teams")
    seen_groups: set[str] = set()
    out = []
    for entry in teams:
        group = str(entry.get("authentikGroup", "")).strip()
        team = str(entry.get("team", "")).strip()
        if not group or not team:
            raise ReconcileError(f"team entry needs authentikGroup and team: {entry!r}")
        # CF lower-cases the claim side when matching, so two spellings of one
        # group would silently collapse to whichever PUT last. Refuse instead.
        key = group.lower()
        if key in seen_groups:
            raise ReconcileError(f"authentikGroup {group!r} declared twice")
        seen_groups.add(key)
        out.append(
            {
                "authentikGroup": group,
                "team": team,
                "description": str(entry.get("description", "")).strip(),
            }
        )
    return out


def _expect(status: int, payload: Any, what: str) -> None:
    if not 200 <= status < 300:
        raise ReconcileError(f"{what} returned HTTP {status}: {str(payload)[:300]}")


def list_teams(api: Api) -> dict[str, str]:
    """Team name -> id for every non-personal team the admin can see."""
    status, payload = api("GET", "/teams/?limit=100", None)
    _expect(status, payload, "GET /teams/")
    items = payload.get("teams", []) if isinstance(payload, dict) else payload
    return {t["name"]: t["id"] for t in items if not t.get("is_personal")}


def ensure_teams(
    api: Api, declared: list[dict[str, str]], log: Callable[[str], None]
) -> dict[str, str]:
    """Return group -> team id, creating any declared team that is missing."""
    existing = list_teams(api)
    mapping: dict[str, str] = {}
    for entry in declared:
        name = entry["team"]
        team_id = existing.get(name)
        if not team_id:
            status, payload = api(
                "POST",
                "/teams/",
                {
                    "name": name,
                    "description": entry["description"]
                    or f"Mapped from the authentik group {entry['authentikGroup']!r}.",
                    "visibility": "private",
                },
            )
            _expect(status, payload, f"POST /teams/ ({name})")
            team_id = (payload or {}).get("id")
            if not team_id:
                raise ReconcileError(
                    f"created team {name!r} but the response carried no id: {payload!r}"
                )
            log(f"created team {name!r} -> {team_id}")
        mapping[entry["authentikGroup"]] = team_id
    return mapping


def reconcile(
    api: Api,
    declared: list[dict[str, str]],
    provider_id: str = "authentik",
    log: Callable[[str], None] = print,
) -> str:
    """Run one reconcile pass. Returns "in-sync" or "updated"; raises on any failure."""
    desired = ensure_teams(api, declared, log)

    status, provider = api("GET", f"/auth/sso/admin/providers/{provider_id}", None)
    if status == 404:
        raise ReconcileError(
            f"sso provider {provider_id!r} is missing: the team_mapping has nothing to hang off. "
            "Create the row with scripts/provision-mcp-auth.sh (needs the authentik CLIENT_ID), then this job converges on its own."
        )
    _expect(status, provider, f"GET /auth/sso/admin/providers/{provider_id}")
    current = provider.get("team_mapping") or {}

    if current == desired:
        log(
            f"team_mapping in sync ({len(desired)} group(s)): {json.dumps(desired, sort_keys=True)}"
        )
        return "in-sync"

    log(
        f"team_mapping drift: have {json.dumps(current, sort_keys=True)} want {json.dumps(desired, sort_keys=True)}"
    )
    status, payload = api(
        "PUT", f"/auth/sso/admin/providers/{provider_id}", {"team_mapping": desired}
    )
    _expect(status, payload, f"PUT /auth/sso/admin/providers/{provider_id}")

    # Re-read rather than trust the PUT's own say-so: the update response omits
    # team_mapping, and a partial apply would otherwise report green.
    status, provider = api("GET", f"/auth/sso/admin/providers/{provider_id}", None)
    _expect(status, provider, f"GET /auth/sso/admin/providers/{provider_id} (verify)")
    if (provider.get("team_mapping") or {}) != desired:
        raise ReconcileError(
            f"PUT reported success but the row reads back {provider.get('team_mapping')!r}, wanted {desired!r}"
        )
    log("team_mapping updated and verified")
    return "updated"


def main() -> int:
    base = os.environ["CF_GATEWAY_URL"]
    token = os.environ["CF_ADMIN_TOKEN"]
    provider_id = os.environ.get("CF_SSO_PROVIDER_ID", "authentik")
    try:
        declared = parse_declared_teams(os.environ["TEAM_MAPPING_JSON"])
        reconcile(http_api(base, token), declared, provider_id)
    except ReconcileError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
