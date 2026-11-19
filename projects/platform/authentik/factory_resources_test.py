"""Factory Authentik server and worker CPU limits render and apply (HOME-131).

A `cpu: null` limit in values-factory.yaml dropped the key in a local
`helm template`, but Argo CD rendered it as a zero limit, which the API
rejects when the request is above it. These tests require an explicit
positive limit at or above the request, and no nulls in the overlay's
resources.
"""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent


def render():
    command = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        "authentik",
        str(CHART),
        "--namespace",
        "authentik",
        "-f",
        str(CHART / "values.yaml"),
        "-f",
        str(CHART / "values-factory.yaml"),
    ]
    return subprocess.run(command, capture_output=True, check=True, timeout=120).stdout


def cores(quantity):
    text = str(quantity)
    if text.endswith("m"):
        return int(text[:-1]) / 1000
    return float(text)


@pytest.mark.parametrize("name", ["authentik-server", "authentik-worker"])
def test_factory_cpu_limit_is_positive_and_covers_the_request(name):
    deployments = {
        doc["metadata"]["name"]: doc
        for doc in yaml.safe_load_all(render())
        if doc and doc["kind"] == "Deployment"
    }
    resources = deployments[name]["spec"]["template"]["spec"]["containers"][0][
        "resources"
    ]
    limit = resources.get("limits", {}).get("cpu")
    assert limit not in (None, ""), f"{name} has no CPU limit"
    assert cores(limit) > 0
    assert cores(limit) >= cores(resources["requests"]["cpu"])


def test_factory_overlay_resources_have_no_nulls():
    overlay = yaml.safe_load((CHART / "values-factory.yaml").read_text())

    def nulls(node, path):
        if isinstance(node, dict):
            for key, value in node.items():
                if value is None and "resources" in path:
                    yield ".".join(path + [key])
                yield from nulls(value, path + [key])

    assert list(nulls(overlay, [])) == []


def test_factory_social_flows_and_application_grants():
    from textwrap import indent
    from types import SimpleNamespace
    from unittest.mock import Mock, patch

    config = next(
        doc["data"]
        for doc in yaml.safe_load_all(render())
        if doc
        and doc["kind"] == "ConfigMap"
        and "factory-social.yaml" in doc.get("data", {})
    )

    def entries(file):
        return yaml.load(config[file], Loader=yaml.BaseLoader)["entries"]

    social = entries("factory-social.yaml")
    by_id = {entry["id"]: entry.get("attrs", {}) for entry in social if "id" in entry}
    bindings = [
        entry["identifiers"]
        for entry in social
        if entry["model"].endswith(".flowstagebinding")
        and entry.get("state") != "absent"
    ]

    def stages(flow):
        return [
            b["stage"]
            for b in sorted(bindings, key=lambda b: int(b["order"]))
            if b["target"] == flow
        ]

    for slug in ("github", "google", "discord"):
        source = by_id[slug]
        assert source["user_matching_mode"] == "identifier"
        assert stages(source["enrollment_flow"]) == ["social-write"]
        assert stages(source["authentication_flow"]) == ["validate", "social-login"]
    assert by_id["validate"]["not_configured_action"] == "configure"
    assert by_id["social-write"]["create_users_group"] == "null"
    assert any(
        e["identifiers"].get("policy") == "social-enrollment-gate"
        and e["identifiers"]["target"] == "social-enrollment"
        for e in social
    )

    def expression(entry, **context):
        exec("def evaluate():\n" + indent(by_id[entry]["expression"], "    "), context)
        return context["evaluate"]()

    client = Mock()
    request = SimpleNamespace(
        context={"source": SimpleNamespace(slug="github"), "oauth_userinfo": {"id": 12}}
    )
    users = Mock()
    users.objects.filter.return_value.exists.return_value = False
    with patch.dict(
        "sys.modules", {"authentik.core.models": SimpleNamespace(User=users)}
    ):
        for verified in (False, True):
            client.get_github_emails.return_value = [
                {"email": "friend@example.org", "primary": True, "verified": verified}
            ]
            profile = expression(
                "github-properties", info={"id": 12}, client=client, token={}
            )
            assert profile["username"] == "github-12"
            request.context["prompt_data"] = profile
            assert (
                expression("social-enrollment-gate", request=request, ak_message=Mock())
                is verified
            )
        users.objects.filter.return_value.exists.return_value = True
        assert not expression(
            "social-enrollment-gate", request=request, ak_message=Mock()
        )
        users.objects.filter.assert_called_with(email__iexact="friend@example.org")

    admins = ["authentik_core.group", ["name", "authentik Admins"]]
    operators = ["authentik_core.group", ["name", "factory-operators"]]
    (group,) = (
        e
        for e in entries("factory-auth.yaml")
        if e["model"] == "authentik_core.group"
        and e["identifiers"]["name"] == "factory-operators"
    )
    assert group["attrs"]["is_superuser"] == "false"
    for file, app, allowed in [
        ("factory-auth.yaml", "edge-app", [admins, operators]),
        ("factory-auth.yaml", "argocd-app", [admins, operators]),
        ("factory-grafana.yaml", "grafana-app", [admins, operators]),
        ("factory-itsaplan.yaml", "itsaplan-app", [admins, operators]),
        ("factory-kubectl.yaml", "kubectl-app", [admins, operators, "kube-viewers"]),
    ]:
        grants = [e for e in entries(file) if e["identifiers"].get("target") == app]
        assert [e["identifiers"]["group"] for e in grants] == allowed
        assert all(
            e["attrs"]["enabled"] == "true" and e["attrs"]["negate"] == "false"
            for e in grants
        )


def test_factory_social_credentials_are_optional_worker_files():
    deployments = {
        doc["metadata"]["name"]: doc["spec"]["template"]["spec"]
        for doc in yaml.safe_load_all(render())
        if doc and doc["kind"] == "Deployment"
    }
    worker = deployments["authentik-worker"]
    volume = next(v for v in worker["volumes"] if v["name"] == "social-credentials")
    assert {
        "name": "social-credentials",
        "mountPath": "/run/factory-social",
        "readOnly": True,
    } in worker["containers"][0]["volumeMounts"]
    sources = [s["secret"] for s in volume["projected"]["sources"]]
    assert {s["name"] for s in sources} == {
        f"factory-authentik-{slug}" for slug in ("github", "google", "discord")
    }
    for secret in sources:
        assert secret["optional"] is True
        slug = secret["name"].removeprefix("factory-authentik-")
        assert secret["items"] == [
            {"key": k, "path": f"{slug}/{k}"} for k in ("client-id", "client-secret")
        ]
    for pod in deployments.values():
        assert not any(
            e["name"].startswith("FACTORY_") for e in pod["containers"][0]["env"]
        )
    assert not any(
        v["name"] == "social-credentials"
        for v in deployments["authentik-server"].get("volumes", [])
    )
