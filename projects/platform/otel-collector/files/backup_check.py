"""Read one CNPG Cluster and export its backup age as an OTLP/HTTP gauge."""

import json
import os
import ssl
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen

SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
METRIC = "cnpg.backup.last_success_age_seconds"


def parse_timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("backup timestamp must include a timezone")
    return parsed


def backup_age(cluster, now):
    successful = cluster.get("status", {}).get("lastSuccessfulBackup")
    timestamp = successful or cluster["metadata"]["creationTimestamp"]
    age = (now - parse_timestamp(timestamp)).total_seconds()
    if age < 0:
        raise ValueError("backup timestamp is in the future")
    return age, bool(successful)


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
    namespace = os.environ["CNPG_NAMESPACE"]
    cluster = os.environ["CNPG_CLUSTER"]
    host = os.environ["KUBERNETES_SERVICE_HOST"]
    if ":" in host:
        host = f"[{host}]"
    port = os.environ["KUBERNETES_SERVICE_PORT"]
    url = (
        f"https://{host}:{port}/apis/postgresql.cnpg.io/v1/namespaces/"
        f"{quote(namespace, safe='')}/clusters/{quote(cluster, safe='')}"
    )
    token = (SERVICE_ACCOUNT / "token").read_text().strip()
    context = ssl.create_default_context(cafile=str(SERVICE_ACCOUNT / "ca.crt"))
    request = Request(url, headers={"Authorization": f"Bearer {token}"})
    with urlopen(request, context=context, timeout=20) as response:
        document = json.load(response)
    age, has_backup = backup_age(document, datetime.now(timezone.utc))
    request = Request(
        os.environ["OTLP_METRICS_ENDPOINT"],
        data=json.dumps(
            payload(cluster, namespace, age, has_backup, time.time_ns())
        ).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=20) as response:
        result = json.load(response)
    partial = result.get("partialSuccess", {})
    if int(partial.get("rejectedDataPoints", 0)) or partial.get("errorMessage"):
        raise RuntimeError("collector rejected backup metric")


def main():
    try:
        run()
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        print(f"backup check failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
