# invariant: PLAT-14
import os
from pathlib import Path
import shutil
import subprocess

import yaml


def test_rendered_host_access_audit(tmp_path):
    resources = tmp_path / "resources"
    shutil.copytree(Path(__file__).parent / "resources", resources)
    output = subprocess.check_output(
        [os.environ.get("KUSTOMIZE_BIN", "kustomize"), "build", resources], text=True
    )
    docs = {doc["kind"]: doc for doc in yaml.safe_load_all(output)}
    policy = docs["ValidatingAdmissionPolicy"]
    binding = docs["ValidatingAdmissionPolicyBinding"]["spec"]
    assert binding["policyName"] == policy["metadata"]["name"]
    assert set(binding["validationActions"]) == {"Audit", "Warn"}
    assert policy["spec"]["failurePolicy"] == "Ignore"
    assert policy["spec"]["matchConstraints"]["resourceRules"] == [
        {
            "apiGroups": [""],
            "apiVersions": ["v1"],
            "operations": ["CREATE", "UPDATE"],
            "resources": ["pods", "pods/ephemeralcontainers"],
        }
    ]
    expressions = " ".join(v["expression"] for v in policy["spec"]["validations"])
    for field in [
        "!object.spec.hostNetwork",
        "!object.spec.hostPID",
        "has(v.hostPath)",
    ]:
        assert field in expressions
    for group in ["containers", "initContainers", "ephemeralContainers"]:
        assert f"object.spec.{group}.all(c, " in expressions
    assert expressions.count("!c.securityContext.privileged") == 3

    (selector,) = binding["matchResources"]["namespaceSelector"]["matchExpressions"]
    assert selector["key"] == "kubernetes.io/metadata.name"
    assert selector["operator"] == "NotIn"
    # HOME-283 hostPath inventory plus the Kubernetes system namespace.
    assert set(selector["values"]) == {
        "ate-system",
        "factory-discord-agent",
        "factory-r-homelab",
        "factory-r-homelab-review",
        "kube-system",
        "otel-collector",
    }
