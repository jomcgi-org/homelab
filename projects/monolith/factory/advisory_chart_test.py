"""Render the actual private and public charts, including exact read bindings."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SIGNALS = {
    "kargoPromotions": "KARGO_PROMOTIONS",
    "embervmControlErrors": "EMBERVM_CONTROL_ERRORS",
    "egressDenials": "EGRESS_DENIALS",
}


def render(public=False, enabled=False, extra=()):
    chart = ROOT.parent / "monolith-public/chart" if public else ROOT / "chart"
    args = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        "monolith",
        str(chart),
        "--namespace",
        "monolith",
        "--set",
        "jobs.image.repository=registry.invalid/jobs",
        "--set-string",
        "jobs.image.digest=sha256:test",
    ]
    if enabled:
        for key in SIGNALS:
            args += ["--set", f"healthAlerts.operationalSignals.{key}.enabled=true"]
    result = subprocess.run(
        args + list(extra), capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def env(docs):
    containers = [
        c
        for d in docs
        if d["kind"] == "Deployment"
        for c in d["spec"]["template"]["spec"]["containers"]
    ]
    return {e["name"]: e.get("value") for c in containers for e in c.get("env", [])}


def test_default_off_every_overlay_and_trace_enablement():
    paths = [
        ROOT / "chart/values.yaml",
        ROOT / "deploy/values.yaml",
        ROOT / "deploy/values-gke.yaml",
        ROOT / "dev/deploy/values.yaml",
        ROOT / "dev/deploy/values-recovery-gke.yaml",
    ]
    for path in paths:
        settings = yaml.safe_load(path.read_text())["healthAlerts"][
            "operationalSignals"
        ]
        assert all(settings[key]["enabled"] is False for key in SIGNALS)
    assert all(
        env(render())[prefix + "_ENABLED"] == "false" for prefix in SIGNALS.values()
    )
    values = env(render(enabled=True))
    assert all(values[prefix + "_ENABLED"] == "true" for prefix in SIGNALS.values())
    assert json.loads(values["EMBERVM_CONTROL_ERRORS_TARGETS"]) == [
        {"namespace": namespace, "selector": "app.kubernetes.io/name=embervm"}
        for namespace in ("embervm", "embervm-dev")
    ]
    assert all(
        "noded-brick" in t["selector"]
        for t in json.loads(values["EGRESS_DENIALS_TARGETS"])
    )


def test_exact_rbac_reads_and_serviceaccount_binding_unchanged():
    off, on = render(), render(enabled=True)
    kinds = {"Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"}
    assert [d for d in off if d["kind"] in kinds] == [
        d for d in on if d["kind"] in kinds
    ]
    role = next(
        d
        for d in on
        if d["kind"] == "ClusterRole" and d["metadata"]["name"] == "monolith"
    )
    for group, resource, verbs in [
        ("kargo.akuity.io", "stages", {"get", "list"}),
        ("kargo.akuity.io", "promotions", {"get", "list"}),
        ("", "pods", {"get", "list"}),
        ("", "pods/log", {"get"}),
    ]:
        rule = next(
            r
            for r in role["rules"]
            if group in r["apiGroups"] and resource in r["resources"]
        )
        assert set(rule["verbs"]) == verbs
    assert not any("secrets" in r["resources"] for r in role["rules"])
    binding = next(
        d
        for d in on
        if d["kind"] == "ClusterRoleBinding" and d["metadata"]["name"] == "monolith"
    )
    assert binding["roleRef"]["name"] == role["metadata"]["name"]
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "monolith", "namespace": "monolith"}
    ]
    deployment = next(
        d
        for d in on
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "monolith"
    )
    assert deployment["spec"]["template"]["spec"]["serviceAccountName"] == "monolith"


def test_public_render_has_no_probe_env_or_cluster_grants():
    docs = render(public=True)
    assert not any(prefix + "_ENABLED" in env(docs) for prefix in SIGNALS.values())
    assert not any(d["kind"] in {"ClusterRole", "ClusterRoleBinding"} for d in docs)


def test_enable_requires_read_access_and_boolean():
    with pytest.raises(AssertionError, match="requires rbac.clusterAccess"):
        render(enabled=True, extra=["--set", "rbac.clusterAccess.enabled=false"])
    with pytest.raises(AssertionError, match="must be a boolean"):
        render(
            extra=[
                "--set-string",
                "healthAlerts.operationalSignals.kargoPromotions.enabled=true",
            ]
        )
