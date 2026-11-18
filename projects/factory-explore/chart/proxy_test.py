import os
from pathlib import Path
import subprocess

import yaml


def test_proxy_boundary():
    output = subprocess.check_output(
        [
            os.environ["HELM_BIN"],
            "template",
            "factory-explore",
            str(Path(__file__).parent),
            "--set",
            "image.digest=sha256:" + "0" * 64,
        ]
    )
    docs = {d["kind"]: d["spec"] for d in yaml.safe_load_all(output)}
    dep = docs["Deployment"]
    assert dep["replicas"] == 1
    assert dep["strategy"]["type"] == "Recreate"
    assert dep["template"]["spec"]["automountServiceAccountToken"] is False
    policy = docs["CiliumNetworkPolicy"]
    assert policy["endpointSelector"]["matchLabels"] == dep["selector"]["matchLabels"]
    (rule,) = policy["ingress"]
    (peer,) = rule["fromEndpoints"]
    assert peer["matchLabels"]["k8s:io.kubernetes.pod.namespace"] == "edge"
    assert peer["matchLabels"]["gateway.envoyproxy.io/owning-gateway-name"] == "edge"
