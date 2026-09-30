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
accepts OTLP only when the shared receiver is enabled by a non-empty service
allowlist. Probe-only renders must remain closed to arbitrary OTLP metrics.

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
    return _render_overlay("values-prod", extra)


def _render_overlay(values_name: str, extra: list[str] | None = None) -> list[dict]:
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
        str(_values(values_name)),
        *(extra or []),
    ]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, f"helm template failed:\n{result.stderr}"
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def _matches_policy(policy: dict, spans: list[dict]) -> bool:
    """Evaluate the focused exact-match collector policy types used here."""
    if policy["type"] == "status_code":
        accepted = set(policy["status_code"]["status_codes"])
        return any(span.get("status_code") in accepted for span in spans)
    if policy["type"] == "string_attribute":
        matcher = policy["string_attribute"]
        accepted = set(matcher["values"])
        return any(
            span.get("attributes", {}).get(matcher["key"]) in accepted for span in spans
        )
    if policy["type"] == "and":
        return all(
            _matches_policy(sub_policy, spans)
            for sub_policy in policy["and"]["and_sub_policy"]
        )
    if policy["type"] == "not":
        return not _matches_policy(policy["not"]["not_sub_policy"], spans)
    raise AssertionError(f"unsupported policy type in focused test: {policy['type']}")


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
    return _named_collector_config(docs, RELEASE)


def _named_collector_config(docs: list[dict], name: str) -> dict:
    """The gateway and the log agent each carry a collector.yaml, so select by
    ConfigMap name rather than taking whichever renders first."""
    for doc in docs:
        if (
            doc.get("kind") == "ConfigMap"
            and doc["metadata"]["name"] == name
            and "collector.yaml" in doc.get("data", {})
        ):
            return yaml.safe_load(doc["data"]["collector.yaml"])
    pytest.fail(f"no ConfigMap {name} carrying collector.yaml in the render")


def _of_kind(docs: list[dict], kind: str) -> dict:
    matches = [d for d in docs if d.get("kind") == kind]
    assert len(matches) == 1, f"expected exactly one {kind}, got {len(matches)}"
    return matches[0]


def _assert_pipelines_reference_defined_components(config: dict) -> None:
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
    config = _collector_config(_render_empty_allowlist())
    metrics = config["service"]["pipelines"]["metrics"]
    assert metrics["receivers"] == ["http_check"]
    assert metrics["exporters"] == ["otlp/honeycomb-metrics"]


# ---------------------------------------------------------------------------
# Populated allowlist: only listed services can reach the shared receiver.
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


def test_prod_allowlist_includes_monolith_emitters():
    values = yaml.safe_load(_values("values-prod").read_text())

    assert {
        "embervm-control",
        "monolith-backend",
        "monolith-jobs",
        "monolith-public",
    } <= set(values["allowedServices"])


@pytest.mark.parametrize("values_name", ["values-prod", "values-gke"])
def test_tail_sampling_keeps_errors_and_pi_runtime_invokes(values_name):
    config = _collector_config(_render_overlay(values_name))
    sampling = config["processors"]["tail_sampling"]

    assert sampling["decision_wait"] == "10s"
    assert sampling["num_traces"] // 10 >= 5_000
    values = yaml.safe_load(_values("values").read_text())
    assert values["sampling"]["tailStorage"]["maxStorageSizeMib"] == 1_536
    policies = {policy["name"]: policy for policy in sampling["policies"]}
    assert policies["keep-errors"] == {
        "name": "keep-errors",
        "type": "and",
        "and": {
            "and_sub_policy": [
                {
                    "name": "error-status",
                    "type": "status_code",
                    "status_code": {"status_codes": ["ERROR"]},
                },
                {
                    "name": "exclude-expected-class",
                    "type": "not",
                    "not": {
                        "not_sub_policy": {
                            "name": "expected-class",
                            "type": "string_attribute",
                            "string_attribute": {
                                "key": "ember.failure.class",
                                "values": ["expected"],
                            },
                        }
                    },
                },
            ]
        },
    }
    assert policies["keep-pi-runtime"] == {
        "name": "keep-pi-runtime",
        "type": "string_attribute",
        "string_attribute": {"key": "ember.workload", "values": ["pi-runtime"]},
    }

    error_trace = [
        {"status_code": "UNSET", "attributes": {"ember.workload": "other"}},
        {
            "status_code": "ERROR",
            "attributes": {"ember.failure.class": "infrastructure"},
        },
    ]
    routine_backpressure_trace = [
        {
            "status_code": "ERROR",
            "attributes": {
                "ember.failure.class": "expected",
                "ember.reason": "queue_full",
            },
        }
    ]
    pi_trace = [
        {"status_code": "UNSET", "attributes": {"ember.workload": "pi-runtime"}}
    ]
    ordinary_trace = [
        {"status_code": "UNSET", "attributes": {"ember.workload": "other"}}
    ]

    assert _matches_policy(policies["keep-errors"], error_trace)
    assert _matches_policy(
        policies["keep-errors"], [{"status_code": "ERROR", "attributes": {}}]
    )
    assert not _matches_policy(policies["keep-errors"], routine_backpressure_trace)
    assert not _matches_policy(policies["keep-pi-runtime"], error_trace)
    assert _matches_policy(policies["keep-pi-runtime"], pi_trace)
    assert not _matches_policy(policies["keep-errors"], pi_trace)
    assert not _matches_policy(policies["keep-errors"], ordinary_trace)
    assert not _matches_policy(policies["keep-pi-runtime"], ordinary_trace)


@pytest.mark.parametrize("values_name", ["values-prod", "values-gke"])
def test_ember_signal_is_capped_and_factory_is_uncapped(values_name):
    """ember.reason / ember.placement.outcome traces are kept, but INSIDE the
    composite ceiling: expected denials scale with caller rate, so an uncapped
    path would let a denial storm bill the month. Factory traces are paced by
    the factory's own heartbeats and are an uncapped peer policy."""
    config = _collector_config(_render_overlay(values_name))
    policies = {p["name"]: p for p in config["processors"]["tail_sampling"]["policies"]}

    # keep-errors must still exclude the expected class; this change must not
    # widen the uncapped path.
    exclude = policies["keep-errors"]["and"]["and_sub_policy"][1]
    assert exclude["not"]["not_sub_policy"]["string_attribute"]["values"] == [
        "expected"
    ]

    factory = policies["keep-factory"]
    assert factory["type"] == "ottl_condition"
    (condition,) = factory["ottl_condition"]["span"]
    assert 'resource.attributes["service.name"] == "monolith-backend"' in condition
    for prefix in ("swarm", "drain", "agent_sessions", "factory"):
        assert prefix in condition

    uncapped_signal = [
        p
        for name, p in policies.items()
        if name != "capped" and "ember.reason" in str(p)
    ]
    assert not uncapped_signal, "ember.reason must not bypass the composite ceiling"

    composite = policies["capped"]["composite"]
    subs = {s["name"]: s for s in composite["composite_sub_policy"]}
    (signal,) = subs["ember-signal"]["ottl_condition"]["span"]
    assert signal == (
        'attributes["ember.reason"] != nil or '
        'attributes["ember.placement.outcome"] != nil'
    )
    # Errors first: composite priority is the sub-policy list order.
    assert [s["name"] for s in composite["composite_sub_policy"]][:2] == [
        "errors",
        "ember-signal",
    ]
    # Every sub-policy has an explicit allocation (a missing one silently
    # falls back to an equal share), each slice holds a whole trace (100
    # spans), and the allocations do not exceed the ceiling.
    alloc = {a["policy"]: a["percent"] for a in composite["rate_allocation"]}
    assert set(alloc) == set(subs)
    assert sum(alloc.values()) <= 100
    total = composite["max_total_spans_per_second"]
    assert all(total * pct / 100 >= 100 for pct in alloc.values())


def test_allowlist_drops_services_not_named():
    """The conditions are OR-ed drop rules, so an unlisted service must be
    dropped by the same condition that keeps a listed one."""
    config = _collector_config(_render(["--set", "allowedServices[0]=monolith"]))
    conditions = config["processors"]["filter/allowlist"]["traces"]["span"]
    joined = " ".join(conditions)
    assert 'resource.attributes["service.name"] != "monolith"' in joined
    assert 'resource.attributes["service.name"] == nil' in joined


def test_metrics_pipeline_accepts_otlp_when_traces_are_on():
    config = _collector_config(_render(["--set", "allowedServices[0]=monolith"]))
    assert config["service"]["pipelines"]["metrics"]["receivers"] == [
        "http_check",
        "otlp",
    ]


def test_metrics_pipeline_uses_otlp_http_exporter_with_dataset_header():
    config = _collector_config(_render(["--set", "honeycomb.protocol=http"]))
    metrics = config["service"]["pipelines"]["metrics"]

    assert metrics["exporters"] == ["otlphttp/honeycomb-metrics"]
    headers = config["exporters"]["otlphttp/honeycomb-metrics"]["headers"]
    assert headers["x-honeycomb-team"] == "${env:HONEYCOMB_API_KEY}"
    assert headers["x-honeycomb-dataset"] == "metrics"


# ---------------------------------------------------------------------------
# HTTP probe staging: legacy targets remain stable while hub HTTPS stays off.
# ---------------------------------------------------------------------------


def test_prod_legacy_probe_targets_are_unchanged():
    config = _collector_config(_render_overlay("values-prod"))

    assert config["receivers"]["http_check"]["targets"] == [
        {"endpoint": "https://jomcgi.dev/health", "method": "GET"},
        {"endpoint": "https://jomcgi.dev/", "method": "GET"},
    ]
    assert config["service"]["pipelines"]["metrics"]["receivers"] == [
        "http_check",
        "otlp",
    ]


def test_mixed_legacy_and_structured_targets_render_together():
    config = _collector_config(_render_overlay("values-test-mixed-targets"))

    assert config["receivers"]["http_check"]["targets"] == [
        {"endpoint": "https://legacy.example.test/healthz", "method": "GET"},
        {
            "endpoint": "https://structured.example.test/healthz",
            "method": "GET",
            "tls": {"ca_file": "/etc/otel/argocd-ca/ca.crt"},
        },
    ]


def test_gke_probe_is_staged_off_with_valid_remaining_pipelines():
    docs = _render_overlay("values-gke")
    config = _collector_config(docs)

    assert "http_check" not in config["receivers"]
    assert config["service"]["pipelines"]["metrics"]["receivers"] == ["otlp"]
    _assert_pipelines_reference_defined_components(config)

    container = _deployment_container(docs)
    assert "httpcheck-ca" not in {m["name"] for m in container["volumeMounts"]}
    volumes = _of_kind(docs, "Deployment")["spec"]["template"]["spec"]["volumes"]
    assert "httpcheck-ca" not in {v["name"] for v in volumes}


def test_gke_stages_https_target_with_verified_ca_and_60s_cadence():
    docs = _render_overlay("values-gke", ["--set", "httpcheck.enabled=true"])
    config = _collector_config(docs)
    receiver = config["receivers"]["http_check"]

    assert receiver["collection_interval"] == "60s"
    assert receiver["targets"] == [
        {
            "endpoint": "https://argocd-server.argocd.svc:443/healthz",
            "method": "GET",
            "tls": {"ca_file": "/etc/otel/argocd-ca/ca.crt"},
        }
    ]
    assert "insecure" not in receiver["targets"][0]["tls"]
    assert "insecure_skip_verify" not in receiver["targets"][0]["tls"]
    _assert_pipelines_reference_defined_components(config)


def test_gke_ca_mount_is_opt_in_read_only_and_key_scoped():
    docs = _render_overlay(
        "values-gke",
        [
            "--set",
            "httpcheck.enabled=true",
            "--set",
            "httpcheck.caMount.enabled=true",
        ],
    )
    deployment = _of_kind(docs, "Deployment")
    pod_spec = deployment["spec"]["template"]["spec"]
    container = pod_spec["containers"][0]
    mount = next(m for m in container["volumeMounts"] if m["name"] == "httpcheck-ca")
    volume = next(v for v in pod_spec["volumes"] if v["name"] == "httpcheck-ca")

    assert mount == {
        "name": "httpcheck-ca",
        "mountPath": "/etc/otel/argocd-ca/ca.crt",
        "subPath": "ca.crt",
        "readOnly": True,
    }
    assert volume["configMap"] == {
        "name": "argocd-server-ca",
        "items": [{"key": "ca.crt", "path": "ca.crt"}],
    }


def test_ca_mount_stays_absent_when_only_mount_flag_is_set():
    docs = _render_overlay("values-gke", ["--set", "httpcheck.caMount.enabled=true"])
    pod_spec = _of_kind(docs, "Deployment")["spec"]["template"]["spec"]

    assert "httpcheck-ca" not in {
        m["name"] for m in pod_spec["containers"][0]["volumeMounts"]
    }
    assert "httpcheck-ca" not in {v["name"] for v in pod_spec["volumes"]}


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
    _assert_pipelines_reference_defined_components(config)


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
    """Keep probes on HTTPS endpoints to cover the public edge.

    An in-cluster target would bypass that coverage. The removed Cilium
    ingress policy is not an enforced reason to choose these endpoints.
    """
    config = _collector_config(_render())
    for target in config["receivers"]["http_check"]["targets"]:
        assert target["endpoint"].startswith("https://"), (
            f"{target['endpoint']} is not a public HTTPS target; in-cluster "
            "probes would bypass public-edge coverage"
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

    # The component type matters, not just that some extension is referenced.
    # tail_storage takes a component.ID, so any id parses and `validate` exits
    # 0, but the processor type-asserts to the TailStorage interface at Start.
    # file_storage implements the byte-KV storage.Extension and fails that
    # assertion with "non-tail-storage extension", after health_check is up.
    assert (
        config["processors"]["tail_sampling"]["tail_storage"] == "pebble_tail_storage"
    ), (
        "tail_storage must name a TailStorage implementation; file_storage "
        "parses fine and then CrashLoops at Start"
    )
    assert "pebble_tail_storage" in config["extensions"]
    assert "pebble_tail_storage" in config["service"]["extensions"], (
        "an extension not listed under service.extensions is never started"
    )

    container = _deployment_container(docs)
    assert any(
        "processor.tailsamplingprocessor.tailstorageextension" in a
        for a in container["args"]
    ), "tail_storage is set but its feature gate is not enabled"

    ext = config["extensions"]["pebble_tail_storage"]
    directory = ext["directory"]
    assert ext.get("max_storage_size_mib"), (
        "max_storage_size_mib defaults to unlimited; without it Pebble can "
        "outgrow the emptyDir and get the pod evicted for ephemeral storage"
    )
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
    assert "pebble_tail_storage" not in config.get("extensions", {})
    assert "pebble_tail_storage" not in config["service"]["extensions"]

    container = _deployment_container(docs)
    assert not any("tailstorageextension" in a for a in container.get("args", []))
    volumes = {
        v["name"]
        for v in _of_kind(docs, "Deployment")["spec"]["template"]["spec"]["volumes"]
    }
    assert "tail-storage" not in volumes


# ---------------------------------------------------------------------------
# Pod log agent: a DaemonSet that must stay inert by default and must never be
# selected by the gateway Service.
# ---------------------------------------------------------------------------

LOGS = f"{RELEASE}-logs"


def test_log_agent_is_absent_by_default():
    for docs in (_render_default(), _render_overlay("values-prod")):
        kinds = {(d["kind"], d["metadata"]["name"]) for d in docs}
        assert ("DaemonSet", LOGS) not in kinds
        assert ("ClusterRole", LOGS) not in kinds
        assert ("ConfigMap", LOGS) not in kinds


def test_log_agent_requires_namespaces():
    argv = [
        os.environ.get("HELM_BIN", "helm"),
        "template",
        RELEASE,
        str(_chart_dir()),
        "--values",
        str(_values("values")),
        "--set",
        "logs.enabled=true",
    ]
    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=120, check=False
    )
    assert result.returncode != 0
    assert "logs.namespaces" in result.stderr


def test_gke_log_agent_pipeline():
    docs = _render_overlay("values-gke")
    config = _named_collector_config(docs, LOGS)
    _assert_pipelines_reference_defined_components(config)

    # The gateway config is untouched by the agent: no logs pipeline there.
    assert "logs" not in _collector_config(docs)["service"]["pipelines"]

    include = config["receivers"]["file_log"]["include"]
    assert "/var/log/pods/embervm_*/*/*.log" in include
    assert "/var/log/pods/kargo-*_*/*/*.log" in include
    assert not any("embervm-dev" in p for p in include)
    assert config["receivers"]["file_log"]["start_at"] == "end"

    logs = config["service"]["pipelines"]["logs"]
    processors = logs["processors"]
    assert processors[0] == "memory_limiter"
    assert processors[-1] == "batch"
    # Parse before filtering, filter before the API-backed enrichment.
    assert (
        processors.index("transform/parse")
        < processors.index("filter/severity")
        < processors.index("k8s_attributes")
    )
    assert config["processors"]["filter/severity"]["logs"]["log_record"] == [
        "log.severity_number < 13"
    ]
    assert config["processors"]["k8s_attributes"]["filter"] == {
        "node_from_env_var": "K8S_NODE_NAME"
    }

    (exporter,) = logs["exporters"]
    assert exporter == "otlphttp/honeycomb-logs"
    headers = config["exporters"][exporter]["headers"]
    assert headers["x-honeycomb-team"] == "${env:HONEYCOMB_API_KEY}"
    assert headers["x-honeycomb-dataset"] == "k8s-logs"


def test_gke_log_agent_workload_shape():
    docs = _render_overlay("values-gke")
    ds = next(d for d in docs if d["kind"] == "DaemonSet")
    pod = ds["spec"]["template"]["spec"]
    labels = ds["spec"]["template"]["metadata"]["labels"]

    # The gateway Service must not select agent pods, or it would send OTLP
    # to pods that do not listen on 4317/4318.
    service_selector = _of_kind(docs, "Service")["spec"]["selector"]
    assert not all(labels.get(k) == v for k, v in service_selector.items())

    assert {"operator": "Exists"} in pod["tolerations"]
    container = pod["containers"][0]
    mounts = {m["name"]: m for m in container["volumeMounts"]}
    assert mounts["varlogpods"]["readOnly"] is True
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["varlogpods"]["hostPath"]["path"] == "/var/log/pods"
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"]["drop"] == ["ALL"]
    assert container["resources"]["limits"]["memory"]
    assert container["resources"]["requests"]["cpu"]

    env = {e["name"]: e for e in container["env"]}
    assert env["HONEYCOMB_API_KEY"]["valueFrom"]["secretKeyRef"] == {
        "name": "honeycomb-ingest",
        "key": "honeycomb-key-secret",
    }

    role = next(d for d in docs if d["kind"] == "ClusterRole")
    for rule in role["rules"]:
        assert set(rule["verbs"]) <= {"get", "list", "watch"}
