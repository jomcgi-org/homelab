"""S9 must exercise the published guest through a bounded, private dev lane."""

from __future__ import annotations

import copy
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml


CHART = Path(__file__).resolve().parent
_RUNNER = "embervm-dev-embervm-conformance"
_CANDIDATE = "sha256:" + "ab" * 32
_VALUES = {
    "dev": ("DEV_VALUES", CHART.parent / "dev/deploy/values.yaml"),
    "dev-gke": ("DEV_GKE_VALUES", CHART.parent / "dev/deploy/values-gke.yaml"),
    "prod": ("PROD_VALUES", CHART.parent / "deploy/values.yaml"),
    "prod-gke": ("PROD_GKE_VALUES", CHART.parent / "deploy/values-gke.yaml"),
    "recovery": (
        "RECOVERY_VALUES",
        CHART.parent / "dev/deploy/values-recovery-gke.yaml",
    ),
}


def _values(*profiles: str) -> list[Path]:
    return [Path(os.environ.get(_VALUES[p][0], _VALUES[p][1])) for p in profiles]


def _helm(
    profiles: tuple[str, ...], settings: list[str], release: str = "embervm-dev"
) -> subprocess.CompletedProcess[str]:
    argv = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        release,
        str(CHART),
        "--namespace",
        release,
    ]
    for path in _values(*profiles):
        argv += ["--values", str(path)]
    for setting in settings:
        argv += ["--set", setting]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _render(
    profiles: tuple[str, ...] = ("dev", "dev-gke"),
    settings: list[str] | None = None,
    release: str = "embervm-dev",
) -> list[dict]:
    result = _helm(profiles, settings or [], release)
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _doc(docs: list[dict], kind: str, name: str) -> dict:
    return next(
        doc for doc in docs if doc["kind"] == kind and doc["metadata"]["name"] == name
    )


def _pod(docs: list[dict], name: str) -> dict:
    return _doc(docs, "Deployment", name)["spec"]["template"]["spec"]


def _env(container: dict) -> dict[str, str]:
    return {
        item["name"]: item["value"]
        for item in container.get("env", [])
        if "value" in item
    }


def _container(pod: dict, name: str) -> dict:
    return next(c for c in pod["containers"] if c["name"] == name)


def _node_pods(docs: list[dict]) -> list[dict]:
    return [
        doc["spec"]["template"]["spec"]
        for doc in docs
        if doc["kind"] in ("Deployment", "DaemonSet")
        and any(
            c["name"] == "noded" for c in doc["spec"]["template"]["spec"]["containers"]
        )
    ]


@pytest.mark.parametrize(
    ("profiles", "release"),
    [
        ((), "embervm-dev"),
        (("dev",), "embervm-dev"),
        (("prod",), "embervm"),
        (("prod", "prod-gke"), "embervm"),
        (("recovery",), "embervm-dev"),
    ],
    ids=["defaults", "home-dev", "home-prod", "hub-prod", "recovery"],
)
def test_codex_probe_is_inert_outside_hub_dev(profiles, release) -> None:
    docs = _render(profiles, release=release)
    for doc in docs:
        if doc["kind"] not in ("Deployment", "DaemonSet"):
            continue
        pod = doc["spec"]["template"]["spec"]
        for container in pod["containers"] + pod.get("initContainers", []):
            env = _env(container)
            assert not any(name.startswith("S9_") for name in env)
            if "ROOTFS_BAKE_FORMAT" in env:
                assert env["ROOTFS_BAKE_FORMAT"] == "b2"
    assert not any(
        doc["metadata"]["name"].endswith("-rootfs-builder-b3") for doc in docs
    )


def test_hub_dev_uses_its_published_guest_and_bounded_native_workload() -> None:
    docs = _render(settings=[f"runtimeClaude.guestImage.digest={_CANDIDATE}"])
    pod = _pod(docs, _RUNNER)
    runner = _container(pod, "conformance")
    env = _env(runner)
    assert {key: value for key, value in env.items() if key.startswith("S9_")} == {
        "S9_CODEX_PROBE": "true",
        "S9_CODEX_WORKLOAD": "claude-runtime",
        "S9_CODEX_MODEL": "terra",
        "S9_CODEX_PROVIDER_MODEL": "gpt-6.1-terra",
        "S9_CODEX_GUEST_DIGEST": _CANDIDATE,
        "S9_CODEX_CLI_VERSION": "0.167.0",
        "S9_BUDGET": "5m",
    }
    assert pod["serviceAccountName"] == _RUNNER
    assert (
        env["EMBERVM_TOKEN_FILE"]
        == "/var/run/secrets/kubernetes.io/serviceaccount/token"
    )
    assert not any(
        "secretKeyRef" in item.get("valueFrom", {}) for item in runner["env"]
    )
    assert runner["securityContext"]["readOnlyRootFilesystem"] is True

    workload = _doc(docs, "Workload", "claude-runtime")["spec"]
    assert workload["source"]["image"]["ref"] == (
        f"ghcr.io/jomcgi/homelab/projects/embervm/runtimes/claude@{_CANDIDATE}"
    )
    assert workload["source"]["image"]["invokePath"] == "/shim/turn"
    assert workload["persistence"]["memory"] is False
    assert workload["persistence"]["filesystem"]["enabled"] is True
    assert workload["persistence"]["filesystem"]["mountPath"] == "/session"
    assert workload["resources"]["memMib"] == 4096
    assert workload["concurrency"] == {"floor": 0, "cap": 1}
    assert workload["session"] == {
        "idleBankSeconds": 20,
        "maxLifetimeSeconds": 600,
        "bankedTtlSeconds": 120,
        "maxSessions": 1,
        "invokeQueueCap": 1,
    }
    assert workload["invocation"]["timeoutSeconds"] == 180


def test_hub_dev_keeps_login_and_token_roles_private_and_egress_narrow() -> None:
    docs = _render()
    broker = _env(
        _container(_pod(docs, "embervm-dev-embervm-tokenbroker"), "tokenbroker")
    )
    assert broker["BROKER_LOGIN_AUTH_REQUIRED"] == "true"
    assert broker["BROKER_RETIRE_PLAINTEXT_PROTECTED_ROUTES"] == "true"
    assert broker["BROKER_LOGIN_SPIFFE_CLIENT_IDS"] == (
        "spiffe://embervm.jomcgi.dev/ns/monolith/sa/monolith"
    )
    assert broker["BROKER_SPIFFE_CLIENT_IDS"] == (
        "spiffe://embervm.jomcgi.dev/ns/embervm-dev/sa/embervm-dev-embervm-noded"
    )
    assert json.loads(broker["TOKENBROKER_GRANTS"]) == [
        {
            "name": "codex-cluster",
            "provider": "codex-chatgpt",
            "fqdn": "auth.openai.com",
        }
    ]
    node_pods = _node_pods(docs)
    assert node_pods
    for pod in node_pods:
        node = _env(_container(pod, "noded"))
        assert node["EMBERVM_NODED_EGRESS_WORKLOADS"] == "claude-runtime"
        proxy = _env(_container(pod, "egress-proxy"))
        assert proxy["EGRESS_INTERNAL_DEFAULT"] == "deny"
        assert not proxy.get("EGRESS_INTERNAL_ALLOWLIST")
        assert proxy["EGRESS_TOKEN_BROKER_URL"] == (
            "https://embervm-dev-embervm-tokenbroker.embervm-dev.svc:8443"
        )
        (secret,) = json.loads(proxy["EGRESS_SECRETS"])
        assert secret["egressTo"] == ["chatgpt.com"]
        assert secret["brokerGrant"] == "codex-cluster"
        assert secret["injectAlwaysPaths"] == ["/backend-api/ps/mcp"]
        assert secret["plaintextUpstream"] is False


def test_runtime_upgrade_needs_only_three_probe_expectation_values() -> None:
    before = _pod(
        _render(
            settings=[
                "conformance.codex.model=sol",
                "conformance.codex.providerModel=gpt-6.1-sol",
                "conformance.codex.cliVersion=0.146.0",
            ]
        ),
        _RUNNER,
    )
    after = _pod(_render(), _RUNNER)
    expected = copy.deepcopy(before)
    replacements = {
        "S9_CODEX_MODEL": "terra",
        "S9_CODEX_PROVIDER_MODEL": "gpt-6.1-terra",
        "S9_CODEX_CLI_VERSION": "0.167.0",
    }
    for item in _container(expected, "conformance")["env"]:
        if item["name"] in replacements:
            item["value"] = replacements[item["name"]]
    assert after == expected


@pytest.mark.parametrize(
    "setting",
    [
        "rootfsBuilder.stampImageDigest=false",
        "tokenBroker.enabled=false",
        "tokenBroker.loginAuth.required=false",
        "tokenBroker.plaintext.retireProtectedRoutes=false",
        "egress.ca.enabled=false",
        "egress.tokenBroker.spiffe.enabled=false",
        "claudeRuntimeWorkload.enabled=false",
        "claudeRuntimeWorkload.persistenceEnabled=false",
        "claudeRuntimeWorkload.memMib=8192",
        "claudeRuntimeWorkload.cap=2",
        "claudeRuntimeWorkload.session.maxSessions=0",
        "claudeRuntimeWorkload.session.invokeQueueCap=2",
        "claudeRuntimeWorkload.session.idleBankSeconds=0",
        "claudeRuntimeWorkload.session.idleBankSeconds=60",
        "claudeRuntimeWorkload.session.maxLifetimeSeconds=601",
        "claudeRuntimeWorkload.session.bankedTtlSeconds=121",
        "claudeRuntimeWorkload.invocation.timeoutSeconds=181",
        "runtimeClaude.guestImage.digest=sha256:bad",
        "conformance.codex.workload=some-other-workload",
        "conformance.codex.model=",
        "conformance.codex.providerModel=",
        "conformance.codex.cliVersion=",
        "conformance.codex.enabled=yes",
    ],
)
def test_probe_rejects_unbounded_or_incomplete_configuration(setting: str) -> None:
    result = _helm(("dev", "dev-gke"), [setting])
    assert result.returncode != 0, setting
    assert "conformance.codex" in result.stderr, result.stderr


def test_probe_refuses_production_namespace() -> None:
    result = _helm(
        ("dev", "dev-gke"), ["conformance.codex.enabled=true"], release="embervm"
    )
    assert result.returncode != 0
    assert "conformance runner is dev-only" in result.stderr


def test_null_optional_codex_section_stays_inert() -> None:
    pod = _pod(_render(("dev",), ["conformance.codex=null"]), _RUNNER)
    assert not any(
        name.startswith("S9_") for name in _env(_container(pod, "conformance"))
    )


@pytest.mark.parametrize("mode", ["legacy", "parallel", "in-pod"])
@pytest.mark.parametrize("stamped", [False, True], ids=["b2", "stamped-b3"])
def test_every_baker_uses_the_same_format_and_path_as_attached_guest(
    mode, stamped
) -> None:
    settings = [
        f"runtimeClaude.guestImage.digest={_CANDIDATE}",
        f"rootfsBuilder.inPodBake.enabled={str(mode == 'in-pod').lower()}",
        f"rootfsBuilder.parallelEnabled={str(mode == 'parallel').lower()}",
        f"rootfsBuilder.stampImageDigest={str(stamped).lower()}",
    ]
    if not stamped:
        settings.append("conformance.codex.enabled=false")
    docs = _render(settings=settings)
    candidate_ref = _doc(docs, "Workload", "claude-runtime")["spec"]["source"]["image"][
        "ref"
    ]
    control = _env(_container(_pod(docs, "embervm-dev-embervm"), "control-plane"))
    identities = dict(
        item.split("=", 1) for item in control["EMBERVM_NODE_IMAGE_IDENTITY"].split(",")
    )
    attached_path = identities[candidate_ref].split("|", 1)[0]
    expected_format = "b3" if stamped else "b2"
    assert attached_path.endswith(
        f"-sha256-{'ab' * 32}-size-4G-format-{expected_format}.ext4"
    )

    node_pods = _node_pods(docs)
    assert node_pods
    for pod in node_pods:
        volume = next(v for v in pod["volumes"] if v["name"] == "rootfs-builder-script")
        cm_name = volume["configMap"]["name"]
        assert cm_name.endswith("-rootfs-builder-b3" if stamped else "-rootfs-builder")
        cm = _doc(docs, "ConfigMap", cm_name)
        if mode == "in-pod":
            builders = [_container(pod, "rootfs-baker")]
            tuples = [
                line.split("\t")
                for line in cm["data"]["rootfs-desired-set"].splitlines()
            ]
            (_, source, built_path, memory) = next(
                row for row in tuples if row[0] == "runtimeClaude"
            )
            assert source == candidate_ref
            assert memory == "4096"
        elif mode == "parallel":
            builders = [
                next(
                    c for c in pod["initContainers"] if c["name"] == "build-all-rootfs"
                )
            ]
            args = builders[0]["args"][2:]
            tuples = [args[i : i + 4] for i in range(0, len(args), 4)]
            (_, source, built_path, memory) = next(
                row for row in tuples if row[0] == "runtimeClaude"
            )
            assert source == candidate_ref
            assert memory == "4096"
        else:
            builders = [
                c for c in pod["initContainers"] if c["name"].startswith("build-")
            ]
            claude = next(
                c for c in builders if _env(c).get("GUEST_IMAGE") == candidate_ref
            )
            built_path = _env(claude)["BASE_ROOTFS_PATH"]
        assert built_path == attached_path
        assert builders
        for builder in builders:
            assert _env(builder)["ROOTFS_BAKE_FORMAT"] == expected_format
            mount = next(
                v
                for v in builder["volumeMounts"]
                if v["name"] == "rootfs-builder-script"
            )
            assert mount["readOnly"] is True


def test_stamped_flag_preserves_the_generic_b2_transition_contract() -> None:
    defaults = yaml.safe_load((CHART / "values.yaml").read_text())
    hub = yaml.safe_load(_values("dev-gke")[0].read_text())
    assert defaults["rootfsBuilder"]["bakeFormatVersion"] == "b2"
    assert defaults["rootfsBuilder"]["stampImageDigest"] is False
    assert hub["rootfsBuilder"] == {"bakeFormatVersion": "b2", "stampImageDigest": True}
    assert defaults["conformance"]["codex"] == {
        "enabled": False,
        "workload": "claude-runtime",
        "model": "terra",
        "providerModel": "gpt-6.1-terra",
        "cliVersion": "0.167.0",
    }
    result = _helm(("dev", "dev-gke"), ["rootfsBuilder.bakeFormatVersion=b3"])
    assert result.returncode != 0
    assert "requires bakeFormatVersion b2 for old-chart compatibility" in result.stderr
