"""Guard the collector's deny-by-default opt-in (#5362).

This chart's whole point is that "nothing is opted in" is ENFORCED by the
rendered config rather than being a convention about what nobody has pointed at
it yet. Quota is the binding constraint, so the invariant is:

  While allowedServices is empty, the collector has NO otlp receiver, NO traces
  pipeline, and NO OTLP port on either the Deployment or the Service. A service
  that dials it gets connection refused, not a silent accept.

That invariant lives entirely in Helm conditionals, and the argocd_app target
only checks that the chart renders at all. So the cheap way for this to regress
is someone adding an otlp receiver back "so the ports are there for later", or
moving the metrics pipeline onto the otlp receiver while debugging. Either diff
reads as harmless and silently re-opens an unmetered path to a paid backend.

The second invariant is narrower and just as load-bearing: the metrics pipeline
takes http_check ONLY. Arbitrary OTLP metrics are the easiest way to burn the
quota by accident, so no service may push them even once it is allowed to send
traces.

These renders need helm: HELM_BIN comes from the BUILD target under Bazel and
falls back to `helm` on PATH locally. The values files are found beside
Chart.yaml, which holds in the repo and in the runfiles tree alike.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

RELEASE = "otel-collector"


def _chart_dir() -> Path:
    here = Path(__file__).resolve().parent
    if (here / "Chart.yaml").exists():
        return here
    raise RuntimeError("Could not find chart Chart.yaml")


def _values(name: str) -> Path:
    """Values files sit beside Chart.yaml, in the repo and in the runfiles tree
    alike, so no env indirection is needed for them."""
    return _chart_dir() / f"{name}.yaml"


def _render_empty_allowlist() -> list[dict]:
    """Prod now opts a service in, so the deny-by-default invariant has to be
    asserted against an explicitly empty list rather than against prod."""
    return _render(["--set", "allowedServices=null"])


def _render(extra: list[str] | None = None) -> list[dict]:
    argv = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        RELEASE,
        str(_chart_dir()),
        "--namespace",
        RELEASE,
        "--values",
        str(_values("values")),
        "--values",
        str(_values("values-prod")),
        *(extra or []),
    ]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, f"helm template failed:\n{result.stderr}"
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def _render_default() -> list[dict]:
    """Render with values.yaml alone, no prod overlay."""
    argv = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        RELEASE,
        str(_chart_dir()),
        "--namespace",
        RELEASE,
        "--values",
        str(_values("values")),
    ]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, f"helm template failed:\n{result.stderr}"
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def _collector_config(docs: list[dict]) -> dict:
    """The collector config is YAML nested inside a ConfigMap string."""
    for doc in docs:
        if doc.get("kind") == "ConfigMap" and "collector.yaml" in doc.get("data", {}):
            return yaml.safe_load(doc["data"]["collector.yaml"])
    pytest.fail("no ConfigMap carrying collector.yaml in the render")


def _of_kind(docs: list[dict], kind: str) -> dict:
    matches = [d for d in docs if d.get("kind") == kind]
    assert len(matches) == 1, f"expected exactly one {kind}, got {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# Empty allowlist: nothing can send.
# ---------------------------------------------------------------------------


def test_empty_allowlist_defines_no_otlp_receiver():
    config = _collector_config(_render_empty_allowlist())
    assert "otlp" not in config["receivers"], (
        "an otlp receiver exists with an empty allowlist: any service that "
        "reaches the collector could ship traces to a paid backend"
    )


def test_empty_allowlist_defines_no_traces_pipeline():
    config = _collector_config(_render_empty_allowlist())
    assert "traces" not in config["service"]["pipelines"]


def test_empty_allowlist_exposes_no_otlp_ports():
    docs = _render_empty_allowlist()
    service_ports = {p["port"] for p in _of_kind(docs, "Service")["spec"]["ports"]}
    assert 4317 not in service_ports and 4318 not in service_ports

    container = _of_kind(docs, "Deployment")["spec"]["template"]["spec"]["containers"][
        0
    ]
    container_ports = {p["containerPort"] for p in container["ports"]}
    assert 4317 not in container_ports and 4318 not in container_ports


def test_empty_allowlist_still_ships_probe_metrics():
    """Deny-by-default must not mean deny-everything: the probes are the whole
    day-one signal, and they are what replaced the SigNoz synthetic monitors."""
    config = _collector_config(_render())
    metrics = config["service"]["pipelines"]["metrics"]
    assert metrics["receivers"] == ["http_check"]
    assert metrics["exporters"] == ["otlp/honeycomb"]


# ---------------------------------------------------------------------------
# Populated allowlist: only listed services, and still no arbitrary metrics.
# ---------------------------------------------------------------------------


def test_allowlisted_service_gets_a_filtered_traces_pipeline():
    docs = _render(["--set", "allowedServices[0]=monolith"])
    config = _collector_config(docs)

    traces = config["service"]["pipelines"]["traces"]
    assert traces["receivers"] == ["otlp"]
    # memory_limiter first (OOM on recovery drain), filter before sampling so
    # a denied service never even reaches the sampler, batch last.
    assert traces["processors"] == [
        "memory_limiter",
        "filter/allowlist",
        "resource/environment",
        "tail_sampling",
        "batch",
    ]

    conditions = config["processors"]["filter/allowlist"]["traces"]["span"]
    assert any("monolith" in c for c in conditions)


def test_allowlist_drops_services_not_named():
    """The conditions are OR-ed drop rules, so an unlisted service must be
    dropped by the same condition that keeps a listed one."""
    config = _collector_config(_render(["--set", "allowedServices[0]=monolith"]))
    conditions = config["processors"]["filter/allowlist"]["traces"]["span"]
    joined = " ".join(conditions)
    assert 'resource.attributes["service.name"] != "monolith"' in joined
    assert 'resource.attributes["service.name"] == nil' in joined


def test_metrics_pipeline_never_accepts_otlp_even_when_traces_are_on():
    config = _collector_config(_render(["--set", "allowedServices[0]=monolith"]))
    assert config["service"]["pipelines"]["metrics"]["receivers"] == ["http_check"], (
        "the metrics pipeline accepts otlp: an opted-in service could push "
        "unbounded metric events, which is the easiest way to burn quota"
    )


# ---------------------------------------------------------------------------
# Config coherence: a pipeline referencing a missing component will not start.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param([], id="empty-allowlist"),
        pytest.param(["--set", "allowedServices[0]=monolith"], id="one-service"),
        pytest.param(
            ["--set", "allowedServices[0]=a", "--set", "allowedServices[1]=b"],
            id="two-services",
        ),
    ],
)
def test_every_pipeline_references_only_defined_components(extra):
    """The collector refuses to start on a dangling reference, which would be
    an ArgoCD-green CrashLoop rather than a render failure."""
    config = _collector_config(_render(extra))
    defined = {
        "receivers": set(config.get("receivers") or {}),
        "processors": set(config.get("processors") or {}),
        "exporters": set(config.get("exporters") or {}),
    }
    for name, pipeline in config["service"]["pipelines"].items():
        for section in ("receivers", "processors", "exporters"):
            missing = set(pipeline.get(section) or []) - defined[section]
            assert not missing, (
                f"pipeline {name} references undefined {section}: {missing}"
            )
        assert pipeline.get("receivers"), f"pipeline {name} has no receivers"


def test_health_route_rewrites_to_the_extension_root():
    """The public route exists so UptimeRobot can metamonitor the collector.

    The health_check extension serves at /, so the /health/otel-collector
    prefix has to be stripped or every probe 404s while the collector is
    perfectly healthy.
    """
    docs = _render()
    routes = [d for d in docs if d.get("kind") == "HTTPRoute"]
    assert len(routes) == 1, "expected exactly one HTTPRoute"
    rule = routes[0]["spec"]["rules"][0]

    assert rule["matches"][0]["path"]["value"] == "/health/otel-collector"
    rewrite = rule["filters"][0]["urlRewrite"]["path"]
    assert rewrite["replacePrefixMatch"] == "/"

    backend = rule["backendRefs"][0]
    assert backend["name"] == "otel-collector"
    assert backend["port"] == 13133, (
        "the route must target the health port, never an OTLP port: those are "
        "not exposed at all while the allowlist is empty"
    )


def test_health_route_is_absent_by_default():
    """Inert without the prod overlay, like every other switch in this chart."""
    argv_docs = _render_default()
    assert not [d for d in argv_docs if d.get("kind") == "HTTPRoute"]


def test_probe_targets_are_public_urls():
    """An in-cluster target needs a matching Cilium ingress rule on the
    destination, or the probe reports a permanent outage that is not
    happening. monolith-api-ingress does not name this namespace, so an
    in-cluster target added without that rule is a false alarm, not a check."""
    config = _collector_config(_render())
    for target in config["receivers"]["http_check"]["targets"]:
        assert target["endpoint"].startswith("https://"), (
            f"{target['endpoint']} is not a public HTTPS target; in-cluster "
            "probes need a Cilium ingress rule added in the same change"
        )


# ---------------------------------------------------------------------------
# Disk-backed tail sampling: three pieces that must move together.
# ---------------------------------------------------------------------------


def _deployment_container(docs):
    return _of_kind(docs, "Deployment")["spec"]["template"]["spec"]["containers"][0]


def test_tail_storage_gate_extension_and_volume_move_together():
    """tail_storage needs all three or the collector will not start.

    Setting the config key without the feature gate fails config validation
    outright. Setting both without a writable mount fails at runtime, because
    readOnlyRootFilesystem is deliberately kept. Each piece is individually
    plausible to drop in a refactor, and any one missing is a CrashLoop that
    ArgoCD still reports Synced.
    """
    docs = _render()
    config = _collector_config(docs)

    assert config["processors"]["tail_sampling"]["tail_storage"] == "file_storage/tail"
    assert "file_storage/tail" in config["extensions"]
    assert "file_storage/tail" in config["service"]["extensions"], (
        "an extension not listed under service.extensions is never started"
    )

    container = _deployment_container(docs)
    assert any(
        "processor.tailsamplingprocessor.tailstorageextension" in a
        for a in container["args"]
    ), "tail_storage is set but its feature gate is not enabled"

    directory = config["extensions"]["file_storage/tail"]["directory"]
    mounts = {m["mountPath"]: m["name"] for m in container["volumeMounts"]}
    assert directory in mounts, (
        f"{directory} is not mounted; readOnlyRootFilesystem makes it unwritable"
    )


def test_tail_storage_buffer_is_ephemeral_and_capped():
    """The buffer holds only pending decisions, so it must not be a PVC, and it
    must be size-capped or a runaway buffer can fill the node and evict
    unrelated pods."""
    docs = _render()
    volumes = {
        v["name"]: v
        for v in _of_kind(docs, "Deployment")["spec"]["template"]["spec"]["volumes"]
    }
    tail = volumes["tail-storage"]

    assert "emptyDir" in tail, "the tail buffer must not be a PersistentVolumeClaim"
    assert tail["emptyDir"].get("sizeLimit"), "an uncapped emptyDir can fill the node"


def test_disabling_tail_storage_removes_every_piece():
    """Turning it off must leave no dangling reference: a tail_storage key with
    the gate off fails validation, which is a worse state than either extreme."""
    docs = _render(["--set", "sampling.tailStorage.enabled=false"])
    config = _collector_config(docs)

    assert "tail_storage" not in config["processors"]["tail_sampling"]
    assert "file_storage/tail" not in config.get("extensions", {})
    assert "file_storage/tail" not in config["service"]["extensions"]

    container = _deployment_container(docs)
    assert not any("tailstorageextension" in a for a in container.get("args", []))
    volumes = {
        v["name"]
        for v in _of_kind(docs, "Deployment")["spec"]["template"]["spec"]["volumes"]
    }
    assert "tail-storage" not in volumes
