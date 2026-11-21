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
    rows = run_preflight(policy)
    assert rows == [
        "factory\tclient\tapi.example.org\texplicit",
        "factory\tclient\tcdn.example.org\twildcard-only",
        "factory\tclient\tmissing.test\tmissing",
    ]


def test_resolver_peer_selectors_against_baseline():
    baseline = yaml.safe_load(
        Path(__file__).with_name("resources").joinpath("baseline.yaml").read_text()
    )
    policy = {
        "kind": "CiliumNetworkPolicy",
        "metadata": {"name": "client", "namespace": "factory"},
        "spec": {
            "endpointSelector": {"matchLabels": {"app": "client"}},
            "egress": [
                {"toFQDNs": [{"matchName": "api.example.org"}]},
                {
                    "toEndpoints": [
                        {
                            "matchLabels": {
                                "k8s:io.kubernetes.pod.namespace": "other-dns",
                                "k8s:k8s-app": "kube-dns",
                            }
                        }
                    ],
                    "toPorts": [
                        {
                            "ports": [{"port": "53"}],
                            "rules": {"dns": [{"matchName": "api.example.org"}]},
                        }
                    ],
                },
            ],
        },
    }
    assert run_preflight(baseline, policy) == [
        "factory\tclient\tapi.example.org\twildcard-only"
    ]

    peer = policy["spec"]["egress"][1]["toEndpoints"][0]
    labels = peer["matchLabels"]
    labels["k8s:io.kubernetes.pod.namespace"] = "kube-system"
    explicit = ["factory\tclient\tapi.example.org\texplicit"]
    assert run_preflight(baseline, policy, expected_exit=0) == explicit

    expressions = [
        {"key": key, "operator": "In", "values": [value]}
        for key, value in labels.items()
    ]
    peer.clear()
    peer["matchExpressions"] = expressions
    assert run_preflight(baseline, policy, expected_exit=0) == explicit

    peer["matchLabels"] = {"k8s:k8s-app": "other-dns"}
    assert run_preflight(baseline, policy) == [
        "factory\tclient\tapi.example.org\twildcard-only"
    ]


def run_preflight(*policies, expected_exit=1):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("dns_preflight.py"))],
        input=yaml.safe_dump_all(policies),
        capture_output=True,
        text=True,
    )
    assert result.returncode == expected_exit, result.stderr
    return result.stdout.splitlines()
