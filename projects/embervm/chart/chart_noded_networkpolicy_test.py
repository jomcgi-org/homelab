"""Evaluate native ingress membership against real rendered workload labels."""

import ipaddress
import os
import subprocess
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent


def render(release=None, environment=None, settings=()):
    release = release or {"dev": "embervm-dev", "prod": "embervm"}.get(
        environment, "policy"
    )
    args = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        release,
        str(CHART),
        "-n",
        release,
    ]
    if environment:
        directory = CHART.parent / ("dev/deploy" if environment == "dev" else "deploy")
        for filename in ("values.yaml", "values-gke.yaml"):
            args += ["-f", str(directory / filename)]
    for setting in settings:
        args += ["--set", setting]
    result = subprocess.run(args, check=True, capture_output=True, text=True)
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def matches(selector, labels):
    return all(
        labels.get(k) == v for k, v in selector.get("matchLabels", {}).items()
    ) and all(
        labels.get(e["key"]) in e["values"]
        for e in selector.get("matchExpressions", [])
    )


def allows(policy, labels, port, namespace=None, protocol="TCP"):
    namespace = namespace or policy["metadata"]["namespace"]
    for rule in policy["spec"]["ingress"]:
        for peer in rule["from"]:
            # Native podSelector-only peers are scoped to the policy namespace.
            same_namespace = namespace == policy["metadata"]["namespace"]
            if same_namespace and matches(peer["podSelector"], labels):
                if any(
                    p.get("protocol", "TCP") == protocol
                    and p["port"] <= port <= p.get("endPort", p["port"])
                    for p in rule["ports"]
                ):
                    return True
    return False


def workloads(documents):
    return [
        d["spec"]["template"]
        for d in documents
        if d["kind"] in {"Deployment", "DaemonSet"}
    ]


@pytest.mark.parametrize("environment", [None, "dev", "prod"])
def test_default_and_checked_in_compositions_are_off(environment):
    assert not any(
        d["kind"] == "NetworkPolicy" for d in render(environment=environment)
    )


@pytest.mark.parametrize("environment", [None, "dev", "prod"])
def test_real_pods_and_callers(environment):
    settings = ["noded.ingressNetworkPolicy.enabled=true"]
    if environment == "dev":
        # The checked-in dev lane has no Envoy or serving workloads. Render
        # the documented validation composition so caller checks are exercised.
        settings += [
            "servingEnvoy.enabled=true",
            "pingWorkload.enabled=true",
            "demoPostgres.enabled=true",
            "scratchPostgres.enabled=true",
            "scratchPostgres.guestImage.repository=ghcr.io/jomcgi/homelab/projects/embervm/runtimes/postgres",
        ]
    docs = render(environment=environment, settings=settings)
    policy = next(d for d in docs if d["kind"] == "NetworkPolicy")
    # Helm leaves namespaced resources' namespace implicit; install scopes them.
    policy["metadata"]["namespace"] = {"dev": "embervm-dev", "prod": "embervm"}.get(
        environment, "policy"
    )
    assert policy["apiVersion"] == "networking.k8s.io/v1"
    assert policy["spec"]["policyTypes"] == ["Ingress"]
    for rule in policy["spec"]["ingress"]:
        assert rule["ports"]
        assert all("port" in p and p["protocol"] == "TCP" for p in rule["ports"])
        assert all(
            set(peer) == {"podSelector"} and peer["podSelector"]["matchLabels"]
            for peer in rule["from"]
        )

    pods = workloads(docs)
    assert any(
        p["metadata"]["labels"].get("app.kubernetes.io/component") == "serving-envoy"
        for p in pods
    )
    if environment == "dev":
        definitions = {
            d["metadata"]["name"]: d["spec"] for d in docs if d["kind"] == "Workload"
        }
        assert definitions["ping"]["class"] == "serving"
        assert definitions["demo-postgres"]["class"] == "stateful"
        assert definitions["demo-postgres"]["stateful"]["listenPort"] == 5401
        assert definitions["scratch-postgres"]["stateful"]["listenPort"] == 5400
    targets = [
        p for p in pods if any(c["name"] == "noded" for c in p["spec"]["containers"])
    ]
    assert targets
    if environment == "prod":
        assert any(
            "embervm.jomcgi.dev/size-class" in p["metadata"]["labels"] for p in targets
        )
    for pod in pods:
        labels = pod["metadata"]["labels"]
        assert matches(policy["spec"]["podSelector"], labels) == (pod in targets)
    for target in targets:
        labels = target["metadata"]["labels"]
        assert not matches(
            policy["spec"]["podSelector"],
            {**labels, "app.kubernetes.io/instance": "other"},
        )
        noded = next(c for c in target["spec"]["containers"] if c["name"] == "noded")
        ports = {p["name"]: p["containerPort"] for p in noded["ports"]}
        env = {e["name"]: e.get("value") for e in noded["env"]}
        base = int(env["EMBERVM_NODED_SERVING_PORT_BASE"])
        subnet = ipaddress.ip_network(env["EMBERVM_NODED_COMPOSITE_SUPERNET"])
        warm_end = min(65535, base + subnet.num_addresses - 2)
        for pod in pods:
            caller = pod["metadata"]["labels"]
            cp = caller.get("app.kubernetes.io/name") == "embervm"
            envoy = caller.get("app.kubernetes.io/component") == "serving-envoy"
            for name in ("grpc", "grpc-tls"):
                if name in ports:
                    assert allows(policy, caller, ports[name]) == cp
            for value in (ports["activator"], 5401, 5410, 5419):
                assert allows(policy, caller, value) == envoy
            for value in (base + 2, base + 254, warm_end):
                assert allows(policy, caller, value) == (cp or envoy)
            for value in (ports["health"], 22, base, base + 1):
                assert not allows(policy, caller, value)
            for value in (ports.get("grpc", 9090), 5401, base + 2):
                assert not allows(policy, caller, value, namespace="unlisted")
                assert not allows(
                    policy, {**caller, "app.kubernetes.io/instance": "other"}, value
                )
                assert not allows(policy, caller, value, protocol="UDP")


def test_overridden_tls_only_ports_ranges_and_release_names():
    docs = render(
        "renamed",
        settings=[
            "noded.ingressNetworkPolicy.enabled=true",
            "nameOverride=custom",
            "noded.plaintextGrpc.enabled=false",
            "noded.spiffe.enabled=true",
            "noded.spiffe.grpcTlsPort=19443",
            "noded.grpcPort=19090",
            "noded.activatorPort=18081",
            "noded.healthPort=18080",
            "noded.servingPortBase=31000",
            "noded.compositeSupernet=10.101.0.0/24",
            "servingEnvoy.statefulTcpPortRange.start=6400",
            "servingEnvoy.statefulTcpPortRange.end=6404",
            "servingEnvoy.compositeTcpPortRange.start=6410",
            "servingEnvoy.compositeTcpPortRange.end=6415",
        ],
    )
    policy = next(d for d in docs if d["kind"] == "NetworkPolicy")
    policy["metadata"]["namespace"] = "renamed"
    cp = next(
        p["metadata"]["labels"]
        for p in workloads(docs)
        if p["metadata"]["labels"]["app.kubernetes.io/name"] == "custom"
    )
    envoy = next(
        p["metadata"]["labels"]
        for p in workloads(docs)
        if p["metadata"]["labels"].get("app.kubernetes.io/component") == "serving-envoy"
    )
    for port in (19443, 31002, 31254):
        assert allows(policy, cp, port, "renamed")
    for port in (19090, 9090, 9443, 8080, 31255):
        assert not allows(policy, cp, port, "renamed")
    for port in (18081, 6400, 6404, 6410, 6415, 31002, 31254):
        assert allows(policy, envoy, port, "renamed")
    for port in (8081, 5401, 6405, 6416, 18080, 19443):
        assert not allows(policy, envoy, port, "renamed")


def test_floor_and_class_labels_without_changing_workload_selectors():
    docs = render(
        settings=[
            "noded.ingressNetworkPolicy.enabled=true",
            "bricks.enabled=true",
            "bricks.nodeFloors[0].node=test-node",
            "bricks.nodeFloors[0].class=2gi",
        ]
    )
    policy = next(d for d in docs if d["kind"] == "NetworkPolicy")
    targets = [
        p
        for p in workloads(docs)
        if any(c["name"] == "noded" for c in p["spec"]["containers"])
    ]
    assert any(
        "embervm.jomcgi.dev/brick-floor" in p["metadata"]["labels"] for p in targets
    )
    assert any(
        p["metadata"]["labels"].get("app.kubernetes.io/component") == "noded-brick"
        for p in targets
    )
    for target in targets:
        assert matches(policy["spec"]["podSelector"], target["metadata"]["labels"])


def test_overridden_plaintext_without_tls():
    docs = render(
        settings=["noded.ingressNetworkPolicy.enabled=true", "noded.grpcPort=19090"]
    )
    policy = next(d for d in docs if d["kind"] == "NetworkPolicy")
    policy["metadata"]["namespace"] = "policy"
    cp = next(
        p["metadata"]["labels"]
        for p in workloads(docs)
        if p["metadata"]["labels"]["app.kubernetes.io/name"] == "embervm"
    )
    assert allows(policy, cp, 19090)
    assert not allows(policy, cp, 9090)
    assert not allows(policy, cp, 9443)


def test_empty_composite_supernet_uses_runtime_fallback():
    docs = render(
        settings=[
            "noded.ingressNetworkPolicy.enabled=true",
            "noded.compositeSupernet=",
        ]
    )
    policy = next(d for d in docs if d["kind"] == "NetworkPolicy")
    policy["metadata"]["namespace"] = "policy"
    callers = [
        p["metadata"]["labels"]
        for p in workloads(docs)
        if p["metadata"]["labels"].get("app.kubernetes.io/name") == "embervm"
        or p["metadata"]["labels"].get("app.kubernetes.io/component") == "serving-envoy"
    ]
    assert len(callers) == 2
    for caller in callers:
        # The second composite group's .10 member uses the /16 offset 256+10.
        for port in (30002, 30254, 30255, 30266, 65535):
            assert allows(policy, caller, port)
        for port in (30000, 30001):
            assert not allows(policy, caller, port)


@pytest.mark.parametrize("composite_supernet", ["10.101.0.0/16", ""])
@pytest.mark.parametrize(
    "settings, listener",
    [
        (["noded.servingPortBase=9000"], "grpcPort"),
        (["noded.grpcPort=30002"], "grpcPort"),
        (["noded.grpcPort=65535"], "grpcPort"),
        (
            ["noded.spiffe.enabled=true", "noded.spiffe.grpcTlsPort=30002"],
            "spiffe.grpcTlsPort",
        ),
        (["noded.healthPort=30002"], "healthPort"),
        (["noded.activatorPort=65535"], "activatorPort"),
        (["servingEnvoy.statefulTcpPortRange.end=30002"], "statefulTcpPortRange"),
        (["servingEnvoy.compositeTcpPortRange.end=30002"], "compositeTcpPortRange"),
    ],
)
def test_warm_range_cannot_overlap_listeners(settings, listener, composite_supernet):
    with pytest.raises(subprocess.CalledProcessError) as error:
        render(
            settings=[
                "noded.ingressNetworkPolicy.enabled=true",
                f"noded.compositeSupernet={composite_supernet}",
                *settings,
            ]
        )
    assert f"{listener} overlaps the warm DNAT range" in error.value.stderr


def test_ports_outside_warm_range_and_disabled_listeners_are_accepted():
    render(
        settings=[
            "noded.ingressNetworkPolicy.enabled=true",
            "noded.compositeSupernet=10.101.0.0/24",
            "noded.healthPort=30001",
            "noded.grpcPort=30255",
            "noded.spiffe.enabled=false",
            "noded.spiffe.grpcTlsPort=30002",
        ]
    )
