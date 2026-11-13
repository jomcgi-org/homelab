"""Default-off Kubernetes observations for private operational advisories.

Only aggregate counts leave this module. See docs/observability.md for the
coverage contract, including why incomplete reads cannot prove recovery.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import ssl
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

WINDOW_S = 900
LOOKBACK_S = 960
LOG_BYTES = 262144
LOG_LINES = 2000
LIST_BYTES = 2097152
MAX_PODS = 12
MAX_STAGES = 64
MAX_PROMOTIONS = 500


def read_error(exc: Exception) -> None:
    status = (
        exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    )
    logger.warning(
        "advisory source read failed: %s status=%s", type(exc).__name__, status
    )


def unknown(detail: str) -> dict:
    return {"ok": True, "status": "unknown", "detail": detail}


def stamp(value) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("naive timestamp")
    return parsed.astimezone(timezone.utc)


def outcome(failed: bool, uncertain: bool, detail: str) -> dict:
    # Positive failure evidence survives another source's read failure.
    if failed:
        return {
            "ok": False,
            "detail": detail + ("; incomplete coverage" if uncertain else ""),
        }
    return (
        unknown(detail + "; incomplete coverage")
        if uncertain
        else {"ok": True, "detail": detail}
    )


class Reader:
    """Bound response allocation as well as server-side list/log selection."""

    def __init__(self, http: httpx.AsyncClient):
        self.http = http

    async def read(self, path: str, params: dict, cap: int) -> tuple[bytes, bool]:
        data = bytearray()
        async with (
            asyncio.timeout(0.8),
            self.http.stream("GET", path, params=params) as response,
        ):
            response.raise_for_status()
            async for chunk in response.aiter_bytes(chunk_size=16384):
                data.extend(chunk[: max(0, cap - len(data))])
                if len(data) >= cap:
                    return bytes(data), True
        return bytes(data), False

    async def listing(self, path: str, limit: int, **params) -> tuple[list, bool]:
        raw, capped = await self.read(path, {"limit": limit, **params}, LIST_BYTES)
        document = json.loads(raw)
        items = document["items"]
        if not isinstance(items, list) or len(items) > limit:
            raise ValueError("invalid list")
        return items, capped or bool(document.get("metadata", {}).get("continue"))


async def client() -> httpx.AsyncClient:
    from kubernetes_asyncio import client as kube, config

    config.load_incluster_config()
    cfg = kube.Configuration.get_default_copy()
    return httpx.AsyncClient(
        base_url=cfg.host,
        verify=ssl.create_default_context(cafile=cfg.ssl_ca_cert),
        headers={"Authorization": await cfg.get_api_key_with_prefix("BearerToken")},
        timeout=httpx.Timeout(3.0),
        limits=httpx.Limits(max_connections=3),
        trust_env=False,
    )


def evaluate_promotions(
    stages: list, promotions: list, now: datetime, incomplete=False
) -> dict:
    """Latest terminal *attempt* per namespace/stage, ordered by creation time.

    Running/Pending attempts never supersede terminal evidence. Equal-time
    conflicting attempts and invalid timestamps cannot establish recovery.
    """
    uncertain = incomplete or not stages
    faults = 0
    groups = {}
    for stage in stages:
        try:
            meta = stage["metadata"]
            key = (meta["namespace"], meta["name"])
            groups[key] = []
        except (KeyError, TypeError):
            uncertain = True
    for item in promotions:
        try:
            key = (item["metadata"]["namespace"], item["spec"]["stage"])
            if key not in groups:
                continue
            created = stamp(item["metadata"]["creationTimestamp"])
            phase = item.get("status", {}).get("phase", "Pending")
            if created > now or phase not in {
                "Pending",
                "Running",
                "Succeeded",
                "Failed",
                "Errored",
                "Aborted",
            }:
                raise ValueError("ambiguous attempt")
            if phase in {"Failed", "Errored", "Succeeded"}:
                groups[key].append((created, phase))
        except (AttributeError, KeyError, TypeError, ValueError):
            uncertain = True
    for attempts in groups.values():
        if not attempts:
            uncertain = True
            continue
        latest = max(t for t, _ in attempts)
        phases = {phase for t, phase in attempts if t == latest}
        if phases & {"Failed", "Errored"}:
            faults += 1
        if len(phases) > 1:
            uncertain = True
    return outcome(
        faults > 0,
        uncertain,
        f"{faults} stage(s) with unresolved Failed/Errored Promotion; {len(groups)} stage(s) inspected",
    )


async def collect_promotions(reader: Reader, now: datetime) -> dict:
    root = "/apis/kargo.akuity.io/v1alpha1"
    stages, stage_partial = await reader.listing(root + "/stages", MAX_STAGES)
    promotions, partial = await reader.listing(root + "/promotions", MAX_PROMOTIONS)
    return evaluate_promotions(stages, promotions, now, stage_partial or partial)


def evaluate_log(
    raw: bytes, capped: bool, kind: str, now: datetime
) -> tuple[int, bool]:
    """Count matching records; require an anchor before the 15-minute window.

    Kubernetes timestamps delimit the window. Producer JSON must also carry a
    valid timestamp; malformed/truncated lines retain earlier positive evidence.
    """
    count = 0
    uncertain = capped or not raw or not raw.endswith(b"\n")
    anchor = False
    previous = None
    lines = raw.splitlines()
    uncertain |= len(lines) >= LOG_LINES
    for line in lines[:LOG_LINES]:
        try:
            prefix, payload = line.decode("utf-8").split(" ", 1)
            observed = stamp(prefix)
            # Reads may include records emitted after the observation started.
            if observed > now:
                continue
            record = json.loads(payload)
            produced = stamp(record["time"])
            if (
                observed < now - timedelta(seconds=LOOKBACK_S)
                or abs((observed - produced).total_seconds()) > 60
            ):
                raise ValueError("invalid clock")
            if previous is not None and observed < previous:
                uncertain = True
            previous = observed
            if observed <= now - timedelta(seconds=WINDOW_S):
                anchor = True
                continue
            level = record["level"]
            message = record["message" if kind == "control" else "msg"]
            if not isinstance(message, str) or not isinstance(level, str):
                raise ValueError("invalid record")
            allowed_levels = (
                {
                    "debug",
                    "info",
                    "notice",
                    "warning",
                    "error",
                    "critical",
                    "alert",
                    "emergency",
                }
                if kind == "control"
                else {"DEBUG", "INFO", "WARN", "ERROR"}
            )
            if level not in allowed_levels:
                raise ValueError("unknown level")
            if kind == "control":
                count += level in {"error", "critical", "alert", "emergency"}
            else:
                count += message in {
                    "egress denied",
                    "egress swap: authority mismatch; request denied",
                    "egress swap: claim injection failed; request denied",
                    "egress swap: credential trailer denied",
                } or message.startswith("egress denied:")
        except (ValueError, KeyError, TypeError, UnicodeError):
            uncertain = True
    return count, uncertain or not anchor


async def collect_logs(reader: Reader, targets: list, kind: str, now: datetime) -> dict:
    uncertain = not targets
    total = 0
    budget = MAX_PODS
    seen = set()
    semaphore = asyncio.Semaphore(3)

    async def one(namespace, pod, container):
        async with semaphore:
            try:
                statuses = pod["status"]["containerStatuses"]
                status = next(s for s in statuses if s["name"] == container)
                started = stamp(status["state"]["running"]["startedAt"])
                partial = started > now - timedelta(seconds=LOOKBACK_S)
                path = f"/api/v1/namespaces/{quote(namespace, safe='')}/pods/{quote(pod['metadata']['name'], safe='')}/log"
                raw, capped = await reader.read(
                    path,
                    {
                        "container": container,
                        "sinceSeconds": LOOKBACK_S,
                        "timestamps": "true",
                        "tailLines": LOG_LINES,
                        "limitBytes": LOG_BYTES,
                    },
                    LOG_BYTES,
                )
                count, incomplete = evaluate_log(raw, capped, kind, now)
                return count, partial or incomplete
            except Exception as exc:  # noqa: BLE001 - API bodies must stay private
                read_error(exc)
                return 0, True

    tasks = []
    for target in targets[:4]:
        try:
            namespace, selector = target["namespace"], target["selector"]
            pods, partial = await reader.listing(
                f"/api/v1/namespaces/{quote(namespace, safe='')}/pods",
                MAX_PODS,
                labelSelector=selector,
            )
            uncertain |= partial or not pods
            for pod in pods:
                identity = (namespace, pod["metadata"]["name"])
                if identity in seen:
                    continue
                seen.add(identity)
                if budget <= 0:
                    uncertain = True
                    break
                budget -= 1
                tasks.append(
                    one(
                        namespace,
                        pod,
                        "control-plane" if kind == "control" else "egress-proxy",
                    )
                )
        except Exception as exc:  # noqa: BLE001 - other targets can still prove failure
            read_error(exc)
            uncertain = True
    uncertain |= len(targets) > 4
    for count, partial in await asyncio.gather(*tasks):
        total += count
        uncertain |= partial
    threshold = 3 if kind == "control" else 1
    return outcome(
        total >= threshold,
        uncertain,
        f"{total} {'control-plane error' if kind == 'control' else 'egress denial'} record(s) in 15m; threshold {threshold}; {len(tasks)} pod(s) inspected",
    )


async def observe(kind: str) -> dict:
    env = {
        "kargo": "KARGO_PROMOTIONS",
        "control": "EMBERVM_CONTROL_ERRORS",
        "egress": "EGRESS_DENIALS",
    }[kind]
    if os.environ.get(env + "_ENABLED", "").lower() != "true":
        return unknown("disabled")
    try:
        async with await client() as http:
            reader = Reader(http)
            now = datetime.now(timezone.utc)
            if kind == "kargo":
                return await collect_promotions(reader, now)
            targets = json.loads(os.environ.get(env + "_TARGETS", "[]"))
            if not isinstance(targets, list):
                raise ValueError("invalid targets")
            return await collect_logs(reader, targets, kind, now)
    except Exception as exc:  # noqa: BLE001 - redact API exception bodies
        read_error(exc)
        return unknown(f"source unreadable ({type(exc).__name__})")


async def kargo_promotions() -> dict:
    return await observe("kargo")


async def embervm_control_errors() -> dict:
    return await observe("control")


async def egress_denials() -> dict:
    return await observe("egress")
