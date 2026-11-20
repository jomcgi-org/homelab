from pathlib import Path

import yaml


def test_gateway_only_admits_trusted_factory_pods():
    # OTLP service.name is supplied by the client. Even an actor claiming a
    # trusted service must be excluded by Cilium before the receiver sees it.
    path = Path(__file__).with_name("resources") / "network-policy.yaml"
    gateway = next(
        doc
        for doc in yaml.safe_load_all(path.read_text())
        if doc["metadata"]["name"] == "trace-gateway"
    )
    assert gateway["metadata"]["namespace"] == "otel-collector"
    spec = gateway["spec"]
    assert spec["endpointSelector"] == {
        "matchLabels": {"app.kubernetes.io/name": "otel-collector"}
    }
    (rule,) = spec["ingress"]
    (peer,) = rule["fromEndpoints"]
    assert set(rule) == {"fromEndpoints", "toPorts"}
    assert peer["matchLabels"] == {"k8s:io.kubernetes.pod.namespace": "factory"}
    (workload,) = peer["matchExpressions"]
    assert workload["key"] == "app.kubernetes.io/name"
    assert workload["operator"] == "In"
    assert set(workload["values"]) == {
        "factory-broker",
        "factory-connector",
        "factory-reviewer",
    }
    assert rule["toPorts"] == [{"ports": [{"port": "4317", "protocol": "TCP"}]}]
