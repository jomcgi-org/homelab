"""Rendered and request-level acceptance guards for public FaaS rate limiting.

The Linux acceptance case renders the active GKE values, runs the resulting
Gateway API resources through the installed Envoy Gateway 1.8.3 translator,
starts the matching Envoy 1.38.3 data plane, and sends real HTTP requests. The
render guards also pin the trusted cloudflared origin boundary and unaffected
public routes.
"""

from __future__ import annotations

import copy
import http.client
import json
import os
import socket
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import yaml

_CRD_TARBALL = "gateway-crds-helm-1.8.3.tgz"
_GATEWAY_TARBALL = "gateway-helm-1.8.3.tgz"
_HOST = "jomcgi.dev"
_FUNCTION_PATH = "/functions/acceptance"
_CROSS_ZONE_WORKER_IP = "2a06:98c0:3600::103"


def _runfile(relative: str) -> Path:
    srcdir = os.environ.get("TEST_SRCDIR", "")
    candidate = Path(srcdir) / "_main" / relative
    if candidate.exists():
        return candidate
    root = Path(__file__).resolve().parents[2]
    local = root / relative
    if local.exists():
        return local
    raise FileNotFoundError(f"could not find {relative} at {candidate} or {local}")


def _configured_path(environment_name: str, fallback: str) -> Path:
    configured = os.environ.get(environment_name)
    if configured and Path(configured).exists():
        return Path(configured)
    return _runfile(fallback)


def _public_chart_dir() -> Path:
    return _runfile("projects/monolith-public/chart/Chart.yaml").parent


def _gateway_chart_dir() -> Path:
    return _runfile("projects/platform/cloudflare-gateway/Chart.yaml").parent


def _render_chart(chart: Path, values: list[Path], *set_values: str) -> list[dict]:
    release_name = "monolith-public" if chart == _public_chart_dir() else chart.name
    command = [
        str(_configured_path("HELM_BIN", "helm")),
        "template",
        release_name,
        str(chart),
        "--namespace",
        "monolith-public" if chart == _public_chart_dir() else "envoy-gateway-system",
    ]
    for value_file in values:
        command.extend(("--values", str(value_file)))
    for value in set_values:
        command.extend(("--set", value))
    result = subprocess.run(command, capture_output=True, check=False, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _render_public(*, accelerated: bool = False) -> list[dict]:
    values = [
        _public_chart_dir() / "values.yaml",
        _configured_path(
            "DEPLOY_VALUES", "projects/monolith-public/deploy/values.yaml"
        ),
        _configured_path(
            "PUBLIC_GKE_VALUES", "projects/monolith-public/deploy/values-gke.yaml"
        ),
    ]
    overrides: tuple[str, ...] = ()
    if accelerated:
        overrides = (
            "cfIngress.public.functionsRateLimit.requests=2",
            "cfIngress.public.functionsRateLimit.unit=Second",
        )
    return _render_chart(_public_chart_dir(), values, *overrides)


def _render_gateway(*, gke: bool) -> list[dict]:
    environment = "GATEWAY_GKE_VALUES" if gke else "GATEWAY_PROD_VALUES"
    fallback = (
        "projects/platform/cloudflare-gateway/values-gke.yaml"
        if gke
        else "projects/platform/cloudflare-gateway/values-prod.yaml"
    )
    return _render_chart(
        _gateway_chart_dir(),
        [_gateway_chart_dir() / "values.yaml", _configured_path(environment, fallback)],
    )


def _by_kind(docs: list[dict], kind: str) -> list[dict]:
    return [doc for doc in docs if doc.get("kind") == kind]


def _named(docs: list[dict], kind: str, name: str) -> dict:
    return next(
        doc
        for doc in docs
        if doc.get("kind") == kind and doc["metadata"]["name"] == name
    )


def _policies_by_target(docs: list[dict]) -> dict[str, dict]:
    return {
        policy["spec"]["targetRefs"][0]["name"]: policy
        for policy in _by_kind(docs, "BackendTrafficPolicy")
    }


def _functions_rules(docs: list[dict]) -> list[dict]:
    policy = _policies_by_target(docs)["monolith-public-functions"]
    return policy["spec"]["rateLimit"]["local"]["rules"]


def _translation_resources(public_docs: list[dict]) -> list[dict]:
    gateway_docs = _render_gateway(gke=True)
    resources: list[dict] = []
    gateway_kinds = {
        "ClientTrafficPolicy",
        "EnvoyPatchPolicy",
        "EnvoyProxy",
        "Gateway",
        "GatewayClass",
    }
    for document in gateway_docs:
        if document.get("kind") in gateway_kinds:
            resources.append(copy.deepcopy(document))

    public_names = {
        ("BackendTrafficPolicy", "monolith-public-functions-rate-limit"),
        ("HTTPRoute", "monolith-public-functions"),
        ("SecurityPolicy", "monolith-public-functions-origin"),
        ("Service", "monolith-public-web"),
        ("BackendTrafficPolicy", "monolith-public-public-rate-limit"),
        ("HTTPRoute", "monolith-public-public"),
        ("SecurityPolicy", "monolith-public-public-origin"),
        ("Service", "monolith-public-frontend"),
    }
    for document in public_docs:
        identity = (document.get("kind"), document.get("metadata", {}).get("name"))
        if identity in public_names:
            copied = copy.deepcopy(document)
            copied["metadata"].setdefault("namespace", "monolith-public")
            resources.append(copied)

    resources.extend(
        [
            {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}}
            for name in ("envoy-gateway-system", "monolith-public")
        ]
    )
    return resources


def _translate(public_docs: list[dict], directory: Path) -> dict:
    source = directory / "gateway-api.yaml"
    source.write_text(yaml.safe_dump_all(_translation_resources(public_docs)))
    result = subprocess.run(
        [
            str(_configured_path("EGCTL_BIN", "egctl")),
            "x",
            "translate",
            "--from",
            "gateway-api",
            "--to",
            "xds",
            "--type",
            "all",
            "--output",
            "json",
            "--file",
            str(source),
            "--namespace",
            "envoy-gateway-system",
            "--add-missing-resources",
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"egctl translation failed: {result.stderr}")
    translated = json.loads(result.stdout)["xds"]
    return translated["envoy-gateway-system/cloudflare-ingress"]


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _static_envoy_config(translated: dict, port: int) -> dict:
    configs = translated["configs"]
    listeners = next(
        item for item in configs if item["@type"].endswith("ListenersConfigDump")
    )["dynamicListeners"]
    listener = copy.deepcopy(
        next(
            item["activeState"]["listener"]
            for item in listeners
            if item["activeState"]["listener"]["name"].endswith(
                "/cloudflare-ingress/http"
            )
        )
    )
    listener.pop("@type")
    listener.pop("accessLog", None)
    listener["address"]["socketAddress"] = {
        "address": "127.0.0.1",
        "portValue": port,
    }

    route_dump = next(
        item for item in configs if item["@type"].endswith("RoutesConfigDump")
    )
    route_config = copy.deepcopy(
        next(
            item["routeConfig"]
            for item in route_dump["dynamicRouteConfigs"]
            if "virtualHosts" in item["routeConfig"]
        )
    )
    route_config.pop("@type")
    for virtual_host in route_config["virtualHosts"]:
        for route in virtual_host["routes"]:
            route.pop("route", None)
            route["directResponse"] = {"status": 200}

    connection_manager = listener["defaultFilterChain"]["filters"][0]["typedConfig"]
    connection_manager.pop("accessLog", None)
    connection_manager.pop("rds")
    connection_manager["routeConfig"] = route_config
    return {
        "staticResources": {"listeners": [listener]},
        "layeredRuntime": {
            "layers": [
                {
                    "name": "gateway-defaults",
                    "staticLayer": {
                        "re2.max_program_size.error_level": 4294967295,
                        "re2.max_program_size.warn_level": 1000,
                    },
                }
            ]
        },
    }


@contextmanager
def _running_envoy(public_docs: list[dict]) -> Iterator[int]:
    with tempfile.TemporaryDirectory() as raw_directory:
        directory = Path(raw_directory)
        port = _free_port()
        translated = _translate(public_docs, directory)
        config = directory / "envoy.yaml"
        config.write_text(yaml.safe_dump(_static_envoy_config(translated, port)))
        envoy = str(_configured_path("ENVOY_BIN", "envoy"))
        validation = subprocess.run(
            [envoy, "--mode", "validate", "-c", str(config)],
            capture_output=True,
            check=False,
            text=True,
        )
        if validation.returncode != 0:
            raise RuntimeError(f"Envoy rejected translated config: {validation.stderr}")

        process = subprocess.Popen(
            [envoy, "--disable-hot-restart", "--log-level", "error", "-c", str(config)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    output = process.stdout.read() if process.stdout else ""
                    raise RuntimeError(f"Envoy exited before readiness: {output}")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                        pass
                    break
                except OSError:
                    time.sleep(0.05)
            else:
                raise RuntimeError("Envoy did not begin accepting requests")
            yield port
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _request(
    port: int,
    identity: str | None = None,
    *,
    path: str = _FUNCTION_PATH,
    worker: str | None = None,
    duplicate_identity: str | None = None,
) -> int:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.putrequest("GET", path, skip_host=True)
    connection.putheader("Host", _HOST)
    if identity is not None:
        connection.putheader("CF-Connecting-IP", identity)
    if duplicate_identity is not None:
        connection.putheader("CF-Connecting-IP", duplicate_identity)
    if worker is not None:
        connection.putheader("CF-Worker", worker)
    connection.endheaders()
    response = connection.getresponse()
    response.read()
    status = response.status
    connection.close()
    return status


def test_pinned_gateway_and_envoy_versions_support_the_rendered_policy():
    with tarfile.open(
        _runfile(f"projects/platform/cloudflare-gateway/charts/{_CRD_TARBALL}"),
        "r:gz",
    ) as archive:
        member = next(
            item
            for item in archive.getmembers()
            if item.name.endswith("backendtrafficpolicies.yaml")
        )
        handle = archive.extractfile(member)
        assert handle is not None
        raw = handle.read().decode()
        crd = yaml.safe_load(
            "\n".join(line for line in raw.splitlines() if not line.startswith("{{-"))
        )
    version = next(
        item for item in crd["spec"]["versions"] if item["name"] == "v1alpha1"
    )
    properties = version["schema"]["openAPIV3Schema"]["properties"]
    header_match = properties["spec"]["properties"]["rateLimit"]["properties"]["local"][
        "properties"
    ]["rules"]["items"]["properties"]["clientSelectors"]["items"]["properties"][
        "headers"
    ]["items"]["properties"]
    assert {"Distinct", "RegularExpression"} <= set(header_match["type"]["enum"])

    with tarfile.open(
        _runfile(f"projects/platform/cloudflare-gateway/charts/{_GATEWAY_TARBALL}"),
        "r:gz",
    ) as archive:
        helper = next(
            item
            for item in archive.getmembers()
            if item.name.endswith("templates/_helpers.tpl")
        )
        handle = archive.extractfile(helper)
        assert handle is not None
        assert 'default "distroless-v1.38.3"' in handle.read().decode()


def test_active_gke_render_scopes_identity_and_preserves_other_budgets():
    documents = _render_public()
    rules = _functions_rules(documents)
    assert len(rules) == 2
    fallback, per_client = rules
    assert "clientSelectors" not in fallback
    assert fallback["limit"] == {"requests": 120, "unit": "Minute"}
    assert per_client["limit"] == fallback["limit"]
    headers = per_client["clientSelectors"][0]["headers"]
    assert headers[0] == {"name": "CF-Connecting-IP", "type": "Distinct"}
    assert headers[1]["name"] == "CF-Connecting-IP"
    assert headers[1]["type"] == "RegularExpression"

    policies = _policies_by_target(documents)
    assert set(policies) == {
        "monolith-public-public",
        "monolith-public-functions",
        "monolith-public-ember-reads",
    }
    page_rules = policies["monolith-public-public"]["spec"]["rateLimit"]["local"][
        "rules"
    ]
    assert len(page_rules) == 2
    page_fallback, page_per_client = page_rules
    assert "clientSelectors" not in page_fallback
    assert page_fallback["limit"] == {"requests": 100, "unit": "Minute"}
    assert page_per_client["limit"] == page_fallback["limit"]
    page_headers = page_per_client["clientSelectors"][0]["headers"]
    assert page_headers[0] == {"name": "CF-Connecting-IP", "type": "Distinct"}
    assert page_headers[1]["name"] == "CF-Connecting-IP"
    assert page_headers[1]["type"] == "RegularExpression"

    assert policies["monolith-public-ember-reads"]["spec"]["rateLimit"]["local"][
        "rules"
    ] == [{"limit": {"requests": 600, "unit": "Minute"}}]

    routes = {doc["metadata"]["name"]: doc for doc in _by_kind(documents, "HTTPRoute")}
    function_paths = [
        match["path"]["value"]
        for rule in routes["monolith-public-functions"]["spec"]["rules"]
        for match in rule["matches"]
    ]
    assert function_paths == ["/functions/"]
    assert "clientSelectors" not in json.dumps(routes["monolith-public-public"])

    security = _named(documents, "SecurityPolicy", "monolith-public-functions-origin")[
        "spec"
    ]["authorization"]
    assert security == {
        "defaultAction": "Allow",
        "rules": [
            {
                "name": "deny-same-zone-workers",
                "action": "Deny",
                "principal": {
                    "headers": [{"name": "CF-Worker", "values": ["jomcgi.dev"]}]
                },
            },
            {
                "name": "deny-cross-zone-workers",
                "action": "Deny",
                "principal": {
                    "headers": [
                        {
                            "name": "CF-Connecting-IP",
                            "values": [_CROSS_ZONE_WORKER_IP],
                        }
                    ]
                },
            },
        ],
    }

    page_security = _named(
        documents, "SecurityPolicy", "monolith-public-public-origin"
    )["spec"]["authorization"]
    assert page_security == security


def test_gateway_render_enforces_the_cloudflared_origin_boundary():
    for gke in (False, True):
        documents = _render_gateway(gke=gke)
        gateway_config = yaml.safe_load(
            _named(documents, "ConfigMap", "envoy-gateway-config")["data"][
                "envoy-gateway.yaml"
            ]
        )
        assert gateway_config["extensionApis"]["enableEnvoyPatchPolicy"] is True

        descriptor_patch = _named(
            documents,
            "EnvoyPatchPolicy",
            "cloudflare-ingress-functions-rate-limit-capacity",
        )
        assert descriptor_patch["metadata"]["namespace"] == "envoy-gateway-system"
        assert descriptor_patch["spec"] == {
            "targetRef": {
                "group": "gateway.networking.k8s.io",
                "kind": "Gateway",
                "name": "cloudflare-ingress",
            },
            "type": "JSONPatch",
            "jsonPatches": [
                {
                    "type": "type.googleapis.com/envoy.config.route.v3.RouteConfiguration",
                    "name": "envoy-gateway-system/cloudflare-ingress/http",
                    "operation": {
                        "op": "add",
                        "jsonPath": (
                            '..routes[?(@.name=~"^httproute/monolith-public/'
                            'monolith-public-functions/")]'
                        ),
                        "path": (
                            "typed_per_filter_config/"
                            "envoy.filters.http.local_ratelimit/"
                            "max_dynamic_descriptors"
                        ),
                        "value": 10000,
                    },
                }
            ],
        }

        page_patch = _named(
            documents,
            "EnvoyPatchPolicy",
            "cloudflare-ingress-page-rate-limit-capacity",
        )
        assert page_patch["metadata"]["namespace"] == "envoy-gateway-system"
        assert page_patch["spec"]["targetRef"] == descriptor_patch["spec"]["targetRef"]
        assert page_patch["spec"]["jsonPatches"][0]["operation"]["jsonPath"] == (
            '..routes[?(@.name=~"^httproute/monolith-public/monolith-public-public/")]'
        )
        assert page_patch["spec"]["jsonPatches"][0]["operation"]["value"] == 10000

        service = _named(documents, "Service", "cloudflare-ingress")
        assert service["spec"]["type"] == "ClusterIP"
        proxy = _named(documents, "EnvoyProxy", "cloudflare-ingress-proxy")
        assert (
            proxy["spec"]["provider"]["kubernetes"]["envoyService"]["type"]
            == "ClusterIP"
        )

        boundary = _named(
            documents, "NetworkPolicy", "cloudflare-ingress-trusted-origin"
        )["spec"]
        assert boundary["podSelector"]["matchLabels"] == service["spec"]["selector"]
        listener_rule = boundary["ingress"][0]
        assert listener_rule["ports"] == [{"protocol": "TCP", "port": 10080}]
        assert listener_rule["from"] == [
            {
                "podSelector": {
                    "matchLabels": {
                        "app.kubernetes.io/name": "cloudflare-tunnel",
                        "app.kubernetes.io/instance": "cloudflare-gateway",
                        "app": "cloudflared",
                    }
                }
            }
        ]
        assert {item["port"] for item in boundary["ingress"][1]["ports"]} == {
            19001,
            19003,
        }

        tunnel = _named(documents, "ConfigMap", "cloudflared")
        assert (
            "service: http://cloudflare-ingress.envoy-gateway-system.svc.cluster.local:80"
            in tunnel["data"]["config.yaml"]
        )
    gke_tunnel = _named(_render_gateway(gke=True), "Deployment", "cloudflared")
    assert gke_tunnel["spec"]["replicas"] == 2


def test_envoy_processes_independent_fallback_and_worker_budgets():
    with _running_envoy(_render_public()) as port:
        abusive = "203.0.113.10"
        unrelated = "192.0.2.200"
        assert [_request(port, abusive) for _ in range(120)] == [200] * 120
        assert _request(port, abusive) == 429

        for address in range(1, 26):
            assert _request(port, f"198.51.100.{address}") == 200
        assert _request(port, abusive) == 429

        assert [_request(port, unrelated) for _ in range(120)] == [200] * 120
        assert _request(port, unrelated) == 429

        assert [_request(port) for _ in range(120)] == [200] * 120
        assert _request(port, "malformed") == 429
        assert (
            _request(
                port,
                "203.0.113.31",
                duplicate_identity="198.51.100.31",
            )
            == 429
        )
        assert _request(port, "2001:db8::20") == 200

        assert _request(port, "192.0.2.40", worker="jomcgi.dev") == 403
        assert _request(port, _CROSS_ZONE_WORKER_IP, worker="other.example") == 403


def test_envoy_refills_the_translated_descriptor_bucket():
    with _running_envoy(_render_public(accelerated=True)) as port:
        identity = "203.0.113.50"
        assert [_request(port, identity) for _ in range(2)] == [200, 200]
        assert _request(port, identity) == 429
        time.sleep(1.1)
        assert _request(port, identity) == 200


def test_envoy_isolates_the_apex_page_budget_per_client():
    with _running_envoy(_render_public()) as port:
        # A scanner sweep on the catch-all page route (Laravel/Ignition probes
        # such as /js/.env fall here) must no longer drain one gateway-wide
        # bucket. Each client owns its own 100/min budget instead.
        scanner = "203.0.113.70"
        assert [
            _request(port, scanner, path="/js/.env") for _ in range(100)
        ] == [200] * 100
        assert _request(port, scanner, path="/js/.env") == 429
        assert _request(port, scanner, path="/") == 429

        # A different client on the same page route is unaffected by the sweep.
        for address in range(1, 11):
            assert _request(port, f"198.51.100.{address}", path="/") == 200

        # A missing identity still has its own fail-safe bucket, separate from
        # any valid client, and the worker-rejection policy covers the route.
        assert _request(port, path="/") == 200
        assert _request(port, "192.0.2.70", worker="jomcgi.dev", path="/") == 403
        assert _request(port, _CROSS_ZONE_WORKER_IP, worker="other.example", path="/") == 403
