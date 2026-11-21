import subprocess
import sys
from pathlib import Path

import yaml


def test_coverage_by_namespace_and_workload():
    policy = {
        "kind": "CiliumNetworkPolicy",
        "metadata": {"name": "client", "namespace": "factory"},
        "spec": {
            "endpointSelector": {"matchLabels": {"app": "client"}},
            "egress": [
                {
                    "toFQDNs": [
                        {"matchName": n}
                        for n in (
                            "db.factory.svc.cluster.local",
                            "api.example.org",
                            "cdn.example.org",
                            "missing.test",
                        )
                    ]
                },
                {
                    "toPorts": [
                        {
                            "ports": [{"port": "53"}],
                            "rules": {
                                "dns": [
                                    {"matchName": "API.example.org."},
                                    {"matchPattern": "*.example.org"},
                                ]
                            },
                        }
                    ]
                },
            ],
        },
    }
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("dns_preflight.py"))],
        input=yaml.safe_dump(policy),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1, result.stderr
    assert result.stdout.splitlines() == [
        "factory\tclient\tapi.example.org\texplicit",
        "factory\tclient\tcdn.example.org\twildcard-only",
        "factory\tclient\tmissing.test\tmissing",
    ]
