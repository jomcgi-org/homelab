"""The backend's traces endpoint has to survive three independent silent drops.

The monolith emitted ZERO traces for as long as SigNoz had been gone, and
nothing anywhere went red. Three defects stacked, each of which fails by
producing no spans and no error:

  1. backend.otelEndpoint was cleared when SigNoz was deleted. The chart only
     renders OTEL_EXPORTER_OTLP_TRACES_ENDPOINT when that value is non-empty,
     and framework/core.py only calls _setup_otel when that env var is set, so
     an empty value disables tracing end to end with no exporter ever built.

  2. The name is coupled across a chart template and a Python gate that never
     see each other. Renaming either side leaves a chart faithfully setting a
     variable nobody reads, or a gate waiting on one nobody sets.

  3. The exporter is opentelemetry.exporter.otlp.proto.http, the HTTP/protobuf
     one, and _setup_otel passes the value as OTLPSpanExporter's `endpoint`
     argument. That argument becomes the POST url VERBATIM: the SDK's
     _append_trace_path fires only on the OTEL_EXPORTER_OTLP_ENDPOINT fallback,
     never on an explicit endpoint. So the value must carry /v1/traces itself
     and must address the collector's HTTP listener on 4318. embervm's
     "http://...:4317" is the gRPC form and is NOT interchangeable: copied
     here it posts HTTP at a gRPC port and drops every span quietly.

None of the three is reachable from a unit test of the app, because all three
live in the gap between the rendered chart and the code that reads it. This
renders the production values and asserts against the source of truth on the
other side of each gap.

A fourth failure mode lives on the far side of the wire and is asserted here
too, because it has the same symptom and no other test spans both charts: the
collector is deny-by-default and its filter processor drops any span whose
service.name is not on allowedServices. Sending correctly to a collector that
discards you is indistinguishable from not sending. Both halves are needed, so
both are checked against each other rather than against a hardcoded string.
"""

from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

import pytest
import yaml

# The collector's HTTP/protobuf listener. 4317 is gRPC on the same Service and
# is the wrong port for this exporter.
_OTLP_HTTP_PORT = 4318
_OTLP_TRACES_PATH = "/v1/traces"

# Namespaces that have been deleted. An endpoint naming one of these resolves
# nowhere, and the OTel SDK fails soft, so it presents as "no traces" rather
# than as an error.
_DEAD_NAMESPACES = ("signoz",)


def _chart_dir() -> Path:
    here = Path(__file__).resolve().parent
    if (here / "Chart.yaml").exists():
        return here
    raise RuntimeError(f"Could not find chart Chart.yaml from {here}")


def _core_py() -> Path:
    """framework/core.py, which holds both the gate and the exporter call.

    Comes from the BUILD target under Bazel; falls back to walking up out of
    the chart directory so the test also runs under plain pytest.
    """
    from_env = os.environ.get("CORE_PY")
    if from_env and Path(from_env).exists():
        return Path(from_env)

    current = _chart_dir().parent
    candidate = current / "framework" / "core.py"
    if candidate.exists():
        return candidate
    raise RuntimeError(f"Could not find framework/core.py from {current}")


def _otlp_endpoint_env_names(source: Path) -> set[str]:
    """Every OTLP-endpoint env var name framework/core.py reads.

    Parsed from the AST rather than grepped so a rename cannot pass by sitting
    in a comment or a docstring. Both the build_app gate and the OTLPSpanExporter
    call read this name, and they must agree with each other as well as with the
    chart: a gate on one name and an exporter on another builds an exporter
    pointed at the SDK default.
    """
    tree = ast.parse(source.read_text(), filename=str(source))
    names: set[str] = set()

    def _record(node: ast.AST) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.startswith("OTEL_") and "ENDPOINT" in node.value:
                names.add(node.value)

    for node in ast.walk(tree):
        # os.environ.get("NAME") and os.environ.get("NAME", default)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "environ"
            and node.args
        ):
            _record(node.args[0])
        # os.environ["NAME"]
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "environ"
        ):
            _record(node.slice)

    return names


def _private_profile_service_name(source: Path) -> str:
    """PRIVATE_PROFILE.service_name, which _setup_otel puts in the resource.

    This is the name the collector's allowlist matches on, so it is read from
    the same file the running code reads it from rather than restated here.
    """
    tree = ast.parse(source.read_text(), filename=str(source))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "PRIVATE_PROFILE" not in targets or not isinstance(node.value, ast.Call):
            continue
        for kw in node.value.keywords:
            if kw.arg == "service_name" and isinstance(kw.value, ast.Constant):
                return kw.value.value
    raise RuntimeError(f"no PRIVATE_PROFILE service_name found in {source}")


def _collector_allowed_services() -> list[str] | None:
    """The collector's prod opt-in list, or None only when it was never provided.

    The BUILD target passes COLLECTOR_PROD_VALUES. When that is SET, a missing
    file is a hard failure rather than a skip: under Bazel the data dependency
    is supposed to be there, and a skip would report as a passing target, which
    is the same false green this whole file exists to prevent. Only a run that
    was never given the file at all, plain pytest outside Bazel with the repo
    layout not matching, degrades to a skip.
    """
    from_env = os.environ.get("COLLECTOR_PROD_VALUES")
    if from_env:
        candidate = Path(from_env)
        assert candidate.exists(), (
            f"COLLECTOR_PROD_VALUES points at {candidate}, which does not exist. "
            "The BUILD target's data dependency on "
            "//projects/platform/otel-collector:prod_values is not reaching the "
            "runfiles tree, so the cross-chart assertions would silently skip."
        )
    else:
        candidate = (
            _chart_dir().parents[1] / "platform" / "otel-collector" / "values-prod.yaml"
        )
        if not candidate.exists():
            return None
    return yaml.safe_load(candidate.read_text()).get("allowedServices") or []


def _render() -> str:
    chart_dir = _chart_dir()
    deploy_values = os.environ.get("DEPLOY_VALUES")
    if not deploy_values:
        deploy_values = str(chart_dir.parent / "deploy" / "values.yaml")
    assert Path(deploy_values).exists(), f"deploy values not found at {deploy_values}"

    result = subprocess.run(
        [
            os.environ.get("HELM_BIN", "helm"),
            "template",
            "monolith",
            str(chart_dir),
            "--namespace",
            "monolith",
            "--values",
            str(chart_dir / "values.yaml"),
            "--values",
            deploy_values,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"helm template failed ({result.returncode}): {result.stderr}"
        )
    return result.stdout


@pytest.fixture(scope="module")
def rendered_env() -> dict[str, str]:
    """Every env var on every container of the backend Deployment.

    Flattened across containers on purpose. The assertions below care that the
    backend process gets the variable, not which container declaration carries
    it, and pinning the container name would make an unrelated rename fail here.
    """
    env: dict[str, str] = {}
    for doc in yaml.safe_load_all(_render()):
        if not doc or doc.get("kind") != "Deployment":
            continue
        spec = doc["spec"]["template"]["spec"]
        for container in spec.get("containers", []) + spec.get("initContainers", []):
            for item in container.get("env", []) or []:
                if "value" in item:
                    env[item["name"]] = item["value"]
    return env


def _endpoint_env_name() -> str:
    """The single OTLP endpoint variable core.py reads.

    Unpacking the set inline would raise a bare ValueError when the gate and the
    exporter drift apart, which is a real defect reported as a confusing one, so
    the singularity is asserted here with the reason attached.
    """
    names = _otlp_endpoint_env_names(_core_py())
    assert len(names) == 1, (
        f"framework/core.py reads {sorted(names)} as OTLP endpoints. It must "
        "read exactly one: the build_app gate and the OTLPSpanExporter argument "
        "have to name the same variable, or the gate opens on one and the "
        "exporter is configured from another."
    )
    return names.pop()


def _endpoint(rendered_env: dict[str, str]) -> str:
    """The rendered endpoint, or a failure naming the cause rather than a KeyError.

    Shared by the assertions below so that a missing variable, which is the
    original defect, reports as the missing variable everywhere instead of as
    incidental breakage in whichever test happened to index it first.
    """
    name = _endpoint_env_name()
    value = rendered_env.get(name, "")
    assert value.strip(), (
        f"the backend Deployment renders no usable {name}. framework/core.py "
        "gates _setup_otel on that variable, so the service builds no exporter "
        "and emits nothing, silently. Set backend.otelEndpoint in "
        "projects/monolith/deploy/values.yaml: an empty value renders no env "
        "var at all."
    )
    return value


def test_core_reads_exactly_one_otlp_endpoint_name():
    """The gate and the exporter must read the SAME variable.

    Guards the guard below: if these two ever disagree, asserting the chart sets
    "the name core.py reads" stops being a well-formed question.
    """
    names = _otlp_endpoint_env_names(_core_py())
    assert names == {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"}, (
        f"framework/core.py reads OTLP endpoint variables {sorted(names)}. It "
        "must read exactly OTEL_EXPORTER_OTLP_TRACES_ENDPOINT: the build_app "
        "gate and the OTLPSpanExporter endpoint have to be the same name, and "
        "OTEL_EXPORTER_OTLP_ENDPOINT is a different variable with different "
        "path-appending semantics."
    )


def test_chart_sets_the_endpoint_name_core_actually_reads(rendered_env):
    """The chart-to-code coupling. This is defect 2, and defect 1 by extension.

    Reading the expected name out of core.py rather than hardcoding it means a
    rename on either side fails here instead of shipping a service that renders
    a variable nobody reads.
    """
    expected = _endpoint_env_name()
    assert expected in rendered_env, (
        f"the backend Deployment does not set {expected}, which is the variable "
        "framework/core.py gates _setup_otel on. Without it build_app skips OTel "
        "entirely and the service exports nothing, with no error anywhere. Check "
        "backend.otelEndpoint in projects/monolith/deploy/values.yaml is set: an "
        "empty value renders no env var at all."
    )
    _endpoint(rendered_env)


def test_endpoint_is_the_http_exporter_form(rendered_env):
    """Defect 3, the syntax trap that drops every span silently.

    The HTTP exporter POSTs this value verbatim, so it needs the scheme, the
    HTTP port and the /v1/traces path. The gRPC form embervm uses has none of
    the last two and is the easy thing to copy.
    """
    name = _endpoint_env_name()
    endpoint = _endpoint(rendered_env)

    assert endpoint.startswith(("http://", "https://")), (
        f"{name} is {endpoint!r}. The HTTP/protobuf exporter passes this to "
        "requests as a url, so it needs a scheme. A bare host:port is the otlp "
        "gRPC exporter's form, not this one."
    )
    assert endpoint.endswith(_OTLP_TRACES_PATH), (
        f"{name} is {endpoint!r}, which does not end in {_OTLP_TRACES_PATH}. "
        "_setup_otel passes this as OTLPSpanExporter's explicit `endpoint` "
        "argument, and the SDK appends the signal path only when falling back "
        "to OTEL_EXPORTER_OTLP_ENDPOINT. An explicit endpoint is used as the "
        "POST url unchanged, so the path has to be spelled out here or every "
        "export 404s."
    )
    assert f":{_OTLP_HTTP_PORT}{_OTLP_TRACES_PATH}" in endpoint, (
        f"{name} is {endpoint!r}, which does not address port "
        f"{_OTLP_HTTP_PORT}. The collector Service exposes 4317 for gRPC and "
        f"{_OTLP_HTTP_PORT} for HTTP; this exporter speaks HTTP. Pointing it at "
        "4317 connects and then fails every export, which looks exactly like "
        "sending no traces at all."
    )


def test_endpoint_names_no_deleted_namespace(rendered_env):
    """The stale-destination check that would have caught this on day one.

    An endpoint in a deleted namespace does not resolve, and the SDK fails soft,
    so the only visible symptom is an empty dataset.
    """
    name = _endpoint_env_name()
    endpoint = _endpoint(rendered_env)
    host = endpoint.split("//", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    parts = host.split(".")

    for dead in _DEAD_NAMESPACES:
        assert dead not in parts, (
            f"{name} is {endpoint!r}, which resolves into the {dead!r} namespace. "
            f"{dead} was deleted, so this name does not resolve and every export "
            "fails silently. Point it at the live collector instead."
        )


def test_backend_service_name_is_on_the_collector_allowlist(rendered_env):
    """Sending is only half of it: the collector must also admit the spans.

    The endpoint and the allowlist live in different charts owned by different
    Applications, and nothing but this test reads both. Drift in either
    direction is silent, because a dropped span and an unsent span look the
    same from Honeycomb.
    """
    allowed = _collector_allowed_services()
    if allowed is None:
        pytest.skip("collector values-prod.yaml not available to this test run")

    service_name = _private_profile_service_name(_core_py())
    assert service_name in allowed, (
        f"the backend reports service.name={service_name!r} but the collector's "
        f"allowedServices is {allowed}. Its filter processor drops every span "
        "whose service.name is not on that list, so the traces are exported "
        "successfully and then discarded on arrival. Add it in "
        "projects/platform/otel-collector/values-prod.yaml."
    )


def test_allowlist_entry_is_pointless_without_an_endpoint(rendered_env):
    """Guard the guard above.

    If the backend ever stops rendering an endpoint, the allowlist assertion
    still passes while no span is ever sent, which is precisely the state this
    whole file exists to make impossible. Tie the two together so the pair
    cannot half-rot.
    """
    allowed = _collector_allowed_services()
    if allowed is None:
        pytest.skip("collector values-prod.yaml not available to this test run")

    name = _endpoint_env_name()
    service_name = _private_profile_service_name(_core_py())
    assert (service_name in allowed) == bool(rendered_env.get(name, "").strip()), (
        f"{service_name!r} is on the collector allowlist but the chart renders "
        f"no {name}, or the reverse. An allowlist entry for a service that "
        "sends nothing is dead config, and an endpoint for a service that is "
        "not allowlisted exports into a filter. Change both or neither."
    )
