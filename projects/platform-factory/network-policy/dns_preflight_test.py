from dns_preflight import inventory


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
    assert list(inventory([policy], "default")) == [
        ("factory", "client", "api.example.org", "explicit"),
        ("factory", "client", "cdn.example.org", "wildcard-only"),
        ("factory", "client", "missing.test", "missing"),
    ]
