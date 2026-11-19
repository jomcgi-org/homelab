import os
from pathlib import Path
import shutil
import subprocess

import pytest
import yaml


def render(*args):
    return list(yaml.safe_load_all(subprocess.check_output(args, text=True)))


def deployment(docs, name):
    return next(
        doc
        for doc in docs
        if doc["kind"] == "Deployment" and doc["metadata"]["name"] == name
    )


def sync_wave(doc):
    return int(
        doc["metadata"].get("annotations", {}).get("argocd.argoproj.io/sync-wave", 0)
    )


def alertmanager_config(docs):
    pod = deployment(docs, "alertmanager")["spec"]["template"]["spec"]
    config = next(
        vol["configMap"]["name"] for vol in pod["volumes"] if vol["name"] == "config"
    )
    cm = next(
        doc
        for doc in docs
        if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == config
    )
    return yaml.safe_load(cm["data"]["alertmanager.yml"])


def test_receiver_cutover(tmp_path):
    # Kustomize's load restriction requires real files inside the base and overlay.
    source = Path(__file__).absolute().parent
    base = shutil.copytree(source.parent / "resources", tmp_path / "resources")
    stack = shutil.copytree(source, tmp_path / "stack")
    chart = base / "alert-relay/chart"
    app = yaml.safe_load((chart.parent / "deploy/application.yaml").read_text())
    assert app["metadata"]["name"] == "prometheus"
    assert {s.get("chart") or s["path"] for s in app["spec"]["sources"]} == {
        "alert-relay",
        "projects/platform-factory/prometheus/stack",
    }
    kustomize = os.environ["KUSTOMIZE_BIN"]
    assert (
        alertmanager_config(render(kustomize, "build", base))["route"]["receiver"]
        == "discord"
    )
    desired = render(kustomize, "build", stack)
    config = alertmanager_config(desired)
    assert config["route"]["receiver"] == "relay"
    assert_grant_inhibitions(config)
    relay = deployment(
        render(
            os.environ["HELM_BIN"],
            "template",
            "alert-relay",
            chart,
            "--set",
            "image.digest=sha256:" + "a" * 64,
        ),
        "alert-relay",
    )
    assert relay["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]
    assert sync_wave(relay) < sync_wave(deployment(desired, "alertmanager"))


def test_alert_links_open_grafana_explore(tmp_path):
    source = Path(__file__).absolute().parent
    base = shutil.copytree(source.parent / "resources", tmp_path / "resources")
    docs = render(os.environ["KUSTOMIZE_BIN"], "build", base)
    prometheus = next(
        doc
        for doc in docs
        if doc["kind"] == "StatefulSet" and doc["metadata"]["name"] == "prometheus"
    )
    pod = prometheus["spec"]["template"]["spec"]
    args = pod["containers"][0]["args"]
    assert "--web.external-url=https://factory-grafana.jomcgi.dev/explore" in args
    assert "--web.route-prefix=/" in args


def assert_grant_inhibitions(config):
    assert config["inhibit_rules"] == [
        {
            "source_matchers": ['alertname="FactoryCodexQuotaExhausted"'],
            "target_matchers": ['alertname="FactoryCodexQuotaLow"'],
            "equal": ["grant"],
        },
        {
            "source_matchers": ['alertname="FactoryCodexGrantNotReady"'],
            "target_matchers": ['alertname=~"FactoryCodexQuota(Low|Exhausted)"'],
            "equal": ["grant"],
        },
    ]


def test_alert_annotations():
    rules = Path(__file__).absolute().parent.parent / "resources/rules.yml"
    for group in yaml.safe_load(rules.read_text())["groups"]:
        for rule in group["rules"]:
            if "alert" in rule:
                assert_alert_annotations(rule)


def assert_alert_annotations(rule):
    for key in ("summary", "action", "graph", "threshold", "runbook"):
        assert rule.get("annotations", {}).get(key, "").strip(), (
            f"{rule['alert']} needs a non-empty {key} annotation"
        )


def test_alert_without_action_fails():
    rule = {
        "alert": "Example",
        "annotations": {
            "summary": "Usage is 95% (threshold 90%)",
            "graph": "usage_percent",
            "threshold": "90",
            "runbook": "https://example.org/runbook",
        },
    }
    with pytest.raises(AssertionError, match="Example needs a non-empty action"):
        assert_alert_annotations(rule)
