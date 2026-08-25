"""The ember-durability health component (#4338, ADR embervm/031).

Reads the EmberVM control plane's GET /v1/health/durability surface, which
classifies durability signals by time-to-impact into two tiers: tier 1 latches
unhealthy on a sustained per-kind artifact-export failure streak (minutes, not
hours: a session parking inside such a window rehydrates blank) and tier 2 on
the newest gc-manifests object aging past 24h + sweep interval. BOTH tiers are
fatal components of this composite (never advisory: an advisory signal pages
nobody, and #4317 is direct proof that a nobody-watching signal goes unread).

Dark by default per the standing rule that a health-affecting detector lands
suspend:true and flips on only after live verification: when
EMBER_DURABILITY_HEALTH_URL is unset the factory returns None and NOTHING is
registered, so shipping this module changes no existing health response.

Vacuous-green guards: a transport failure, a non-200, an unparseable body, or
a 200 body without an explicit ``ok: true`` all read NOT ok. Absence of signal
is never evidence of health.
"""

from __future__ import annotations

import json
import logging
import os

import httpx

from shared.k8s_auth import auth_headers

logger = logging.getLogger(__name__)

ENV_URL = "EMBER_DURABILITY_HEALTH_URL"

CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 10.0


def configured_url() -> str:
    """The configured CP durability endpoint, or "" while dark."""
    return os.environ.get(ENV_URL, "").strip()


def build_durability_health(
    url: str | None = None,
    transport: httpx.BaseTransport | None = None,
):
    """Build the ember-durability component, or None while unconfigured.

    ``url``/``transport`` are injectable so tests can drive every branch
    against httpx.MockTransport without a live control plane.
    """
    resolved = configured_url() if url is None else url
    if not resolved:
        return None

    async def check() -> dict:
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
                transport=transport,
            ) as client:
                resp = await client.get(resolved, headers=auth_headers())
        except httpx.HTTPError as exc:
            logger.warning(
                "ember-durability endpoint unreachable at %s: %s", resolved, exc
            )
            return {"ok": False, "detail": f"durability endpoint unreachable: {exc}"}

        if resp.status_code != 200:
            # The CP answers 503 WITH the full report body when a tier is not
            # ok, so surface the tier verdicts rather than just the status.
            # 404 means the detector is dark on the CP side (suspend:true).
            detail = f"durability endpoint returned HTTP {resp.status_code}"
            try:
                payload = resp.json()
                if isinstance(payload, dict):
                    parts = [
                        f"{name}={tier.get('verdict')}: {tier.get('detail')}"
                        for name, tier in (
                            ("tier1", payload.get("tier1")),
                            ("tier2", payload.get("tier2")),
                        )
                        if isinstance(tier, dict)
                    ]

                    if parts:
                        detail = f"{detail}; " + "; ".join(parts)
                    elif payload.get("error"):
                        detail = f"{detail}: {payload['error']}"
            except (json.JSONDecodeError, ValueError):
                pass

            return {"ok": False, "detail": detail}

        try:
            payload = resp.json()
        except (json.JSONDecodeError, ValueError):
            return {
                "ok": False,
                "detail": "durability endpoint returned a non-JSON body",
            }

        if not isinstance(payload, dict):
            return {
                "ok": False,
                "detail": "durability endpoint returned a non-object body",
            }

        tier1, tier2 = payload.get("tier1"), payload.get("tier2")

        if (
            payload.get("ok") is True
            and isinstance(tier1, dict)
            and tier1.get("ok") is True
            and isinstance(tier2, dict)
            and tier2.get("ok") is True
        ):
            return {
                "ok": True,
                "detail": f"durability ok (tier1={tier1.get('verdict')}, tier2={tier2.get('verdict')})",
            }

        # Anything else (an explicit ok:false, missing fields, an unknown shape)
        # reads NOT ok: absence of signal is never evidence of health.
        parts = []
        for name, tier in (("tier1", tier1), ("tier2", tier2)):
            if isinstance(tier, dict):
                parts.append(f"{name}={tier.get('verdict')}: {tier.get('detail')}")
            else:
                parts.append(f"{name}=missing")

        return {"ok": False, "detail": "; ".join(parts)}

    return check
