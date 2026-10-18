"""Collect named CNPG Clusters once for OTLP and private advisory health."""

import json
import os
import re
import signal
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
METRIC = "cnpg.backup.last_success_age_seconds"
MAX_TARGETS = 4
REQUEST_TIMEOUT_S = 10
MAX_RESPONSE_BYTES = 1024 * 1024
NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?\Z")


def parse_timestamp(value):
    if not isinstance(value, str) or not value:
        raise ValueError("missing timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("backup timestamp must include a timezone")
    return parsed


def backup_age(cluster, now):
    successful = cluster.get("status", {}).get("lastSuccessfulBackup")
    if successful is not None and not isinstance(successful, str):
        raise ValueError("invalid backup timestamp type")
    timestamp = successful or cluster["metadata"]["creationTimestamp"]
    age = (now - parse_timestamp(timestamp)).total_seconds()
    if age < 0:
        raise ValueError("backup timestamp is in the future")
    return age, bool(successful)


def targets_from_env():
    targets = json.loads(os.environ["CNPG_TARGETS"])
    if not isinstance(targets, list) or not 1 <= len(targets) <= MAX_TARGETS:
        raise ValueError("expected one to four targets")
    seen = set()
    for target in targets:
        if not isinstance(target, dict) or set(target) != {"namespace", "cluster"}:
            raise ValueError("invalid target")
        for value in target.values():
            if (
                not isinstance(value, str)
                or len(value) > 63
                or not NAME.fullmatch(value)
            ):
                raise ValueError("invalid target name")
        identity = (target["namespace"], target["cluster"])
        if identity in seen:
            raise ValueError("duplicate target")
        seen.add(identity)
    return targets


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a ServiceAccount bearer to a redirect destination.
        raise RuntimeError("redirect refused")


def _timeout(_signum, _frame):
    raise TimeoutError("request deadline exceeded")


def request_json(request, context=None):
    """Wall-clock and socket bounds, including response reads, with no retries."""
    opener = build_opener(
        ProxyHandler({}), HTTPSHandler(context=context), _NoRedirect()
    )
    previous = signal.signal(signal.SIGALRM, _timeout)
    signal.setitimer(signal.ITIMER_REAL, REQUEST_TIMEOUT_S)
    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_S) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("response too large")
        document = json.loads(raw)
        if not isinstance(document, dict):
            raise ValueError("response is not an object")
        return document
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def payload(cluster, namespace, age, has_backup, time_ns):
    return {
        "resourceMetrics": [
            {
                "resource": {
                    "attributes": [
                        {
                            "key": "service.name",
                            "value": {"stringValue": "cnpg-backup-check"},
                        }
                    ]
                },
                "scopeMetrics": [
                    {
                        "scope": {"name": "cnpg-backup-check"},
                        "metrics": [
                            {
                                "name": METRIC,
                                "unit": "s",
                                "gauge": {
                                    "dataPoints": [
                                        {
                                            "timeUnixNano": str(time_ns),
                                            "asDouble": age,
                                            "attributes": [
                                                {
                                                    "key": "cnpg.cluster.name",
                                                    "value": {"stringValue": cluster},
                                                },
                                                {
                                                    "key": "k8s.namespace.name",
                                                    "value": {"stringValue": namespace},
                                                },
                                                {
                                                    "key": "cnpg.backup.has_successful_backup",
                                                    "value": {"boolValue": has_backup},
                                                },
                                            ],
                                        }
                                    ]
                                },
                            }
                        ],
                    }
                ],
            }
        ]
    }


def run():
    targets = targets_from_env()
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    if ":" in host:
        host = f"[{host}]"
    port = os.environ["KUBERNETES_SERVICE_PORT"]
    api = f"https://{host}:{port}"
    token = (SERVICE_ACCOUNT / "token").read_text().strip()
    context = ssl.create_default_context(cafile=str(SERVICE_ACCOUNT / "ca.crt"))
    observations = []
    failed = False
    for target in targets:
        namespace, cluster = target["namespace"], target["cluster"]
        observation = dict(target, status="error", export_ok=False)
        stage = "kubernetes_read"
        try:
            url = (
                f"{api}/apis/postgresql.cnpg.io/v1/namespaces/"
                f"{quote(namespace, safe='')}/clusters/{quote(cluster, safe='')}"
            )
            document = request_json(
                Request(url, headers={"Authorization": f"Bearer {token}"}), context
            )
            stage = "invalid_observation"
            now = datetime.now(timezone.utc)
            age, has_backup = backup_age(document, now)
            observation.update(
                status="observed",
                observed_at=now.isoformat(),
                age_seconds=age,
                has_successful_backup=has_backup,
                last_successful_backup=(
                    document["status"]["lastSuccessfulBackup"] if has_backup else None
                ),
            )
            stage = "metric_export"
            result = request_json(
                Request(
                    os.environ["OTLP_METRICS_ENDPOINT"],
                    data=json.dumps(
                        payload(cluster, namespace, age, has_backup, time.time_ns())
                    ).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
            )
            partial = result.get("partialSuccess", {})
            if (
                not isinstance(partial, dict)
                or int(partial.get("rejectedDataPoints", 0))
                or partial.get("errorMessage")
            ):
                raise RuntimeError("collector rejected backup metric")
            observation["export_ok"] = True
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
            # Response bodies and exception strings can contain credentials.
            # Only bounded identities and fixed stage names cross this boundary.
            failed = True
            observation["error"] = stage
            print(
                f"backup check failed: {namespace}/{cluster}: {stage}", file=sys.stderr
            )
        observations.append(observation)

    status_name = os.environ.get("CNPG_STATUS_CONFIGMAP", "")
    if status_name:
        namespace = os.environ["CNPG_STATUS_NAMESPACE"]
        url = f"{api}/api/v1/namespaces/{quote(namespace, safe='')}/configmaps/{quote(status_name, safe='')}"
        snapshot = {"version": 1, "targets": observations}
        # PATCH only the precreated object. No create, list, or application route.
        request_json(
            Request(
                url,
                data=json.dumps(
                    {"data": {"observation.json": json.dumps(snapshot)}}
                ).encode(),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/merge-patch+json",
                },
                method="PATCH",
            ),
            context,
        )
    return 1 if failed else 0


def main():
    try:
        return run()
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
        print(
            "backup check failed: configuration or status handoff unavailable",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
