"""Check the Pod Security default and that every node gets the file."""

from pathlib import Path

import yaml

HERE = Path(__file__).parent
TARGET = "/etc/rancher/k3s/pod-security.yaml"


def pod_security():
    config = yaml.safe_load((HERE / "pod-security.yaml").read_text())
    (plugin,) = config["plugins"]
    assert plugin["name"] == "PodSecurity"
    return plugin["configuration"]


def test_default_level_is_baseline():
    defaults = pod_security()["defaults"]
    assert {defaults[mode] for mode in ("enforce", "audit", "warn")} == {"baseline"}


def test_exemptions_are_the_reviewed_namespaces():
    exemptions = pod_security()["exemptions"]
    assert exemptions["namespaces"] == ["kube-system", "ate-system", "otel-collector", "t3"]
    assert not exemptions["usernames"]
    assert not exemptions["runtimeClasses"]


def test_api_server_reads_the_installed_file():
    k3s = yaml.safe_load((HERE / "k3s-config.yaml").read_text())
    assert f"admission-control-config-file={TARGET}" in k3s["kube-apiserver-arg"]
    assert f'"${{HERE}}/pod-security.yaml" {TARGET}' in (HERE / "install-node.sh").read_text()
    assert ",pod-security.yaml," in (HERE / "bootstrap-nodes.sh").read_text()
