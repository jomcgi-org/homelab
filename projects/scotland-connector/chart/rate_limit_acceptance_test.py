"""Request-level acceptance for the connector route's rate limits.

Renders the hub gateway and this chart with the deployed values, translates
them with the pinned Envoy Gateway 1.8.3 translator, starts the pinned Envoy
1.38.3 with the translated listener and routes (backends replaced by a direct
200), and sends real requests carrying CF-Connecting-IP. This is what proves
the bucket rules do what the template comments claim: per-client buckets for
ordinary callers, one shared bucket for assistant egress ranges that never
also draws from a per-client bucket, a fail-safe bucket for a missing or
malformed identity, and Cloudflare Workers refused.

The harness follows projects/monolith/public_faas_rate_limit_test.py.
"""

from __future__ import annotations

import copy
import http.client
import ipaddress
import json
import os
import re
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import yaml

_HOST = "jomcgi.dev"
_PATH = "/scotland/v1/places/resolve?q=Oban"
_CROSS_ZONE_WORKER_IP = "2a06:98c0:3600::103"
_CHART_DIR = Path(__file__).resolve().parent
_PROJECT = _CHART_DIR.parent
_REPO = _PROJECT.parent.parent
_GATEWAY_DIR = _REPO / "projects" / "platform" / "cloudflare-gateway"
_DEPLOY_VALUES = Path(
    os.environ.get("DEPLOY_VALUES", _PROJECT / "deploy" / "values.yaml")
)
_METER_SOURCE = _PROJECT / "internal" / "meter" / "meter.go"


def _bin(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _helm(chart: Path, release: str, namespace: str, values: list[Path], *sets: str):
    command = [_bin("HELM_BIN", "helm"), "template", release, str(chart)]
    command += ["--namespace", namespace]
    for value_file in values:
        command += ["--values", str(value_file)]
    for value in sets:
        command += ["--set", value]
    result = subprocess.run(command, capture_output=True, check=False, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _render_connector(*sets: str) -> list[dict]:
    return _helm(
        _CHART_DIR,
        "scotland-connector",
        "scotland-connector",
        [_DEPLOY_VALUES],
        "route.enabled=true",
        *sets,
    )


def _render_gateway() -> list[dict]:
    return _helm(
        _GATEWAY_DIR,
        "cloudflare-gateway",
        "envoy-gateway-system",
        [_GATEWAY_DIR / "values.yaml", _GATEWAY_DIR / "values-gke.yaml"],
    )


def _resources(connector_docs: list[dict]) -> list[dict]:
    keep = {"ClientTrafficPolicy", "EnvoyPatchPolicy", "EnvoyProxy", "Gateway"}
    keep.add("GatewayClass")
    resources = [
        copy.deepcopy(doc) for doc in _render_gateway() if doc.get("kind") in keep
    ]
    route_kinds = {"BackendTrafficPolicy", "HTTPRoute", "SecurityPolicy", "Service"}
    resources += [
        copy.deepcopy(doc) for doc in connector_docs if doc["kind"] in route_kinds
    ]
    resources += [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name}}
        for name in ("envoy-gateway-system", "scotland-connector")
    ]
    return resources


def _translate(connector_docs: list[dict], directory: Path) -> dict:
    source = directory / "gateway-api.yaml"
    source.write_text(yaml.safe_dump_all(_resources(connector_docs)))
    result = subprocess.run(
        [
            _bin("EGCTL_BIN", "egctl"),
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
    return json.loads(result.stdout)["xds"]["envoy-gateway-system/cloudflare-ingress"]


def _static_config(translated: dict, port: int) -> dict:
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
    listener["address"]["socketAddress"] = {"address": "127.0.0.1", "portValue": port}
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
    manager = listener["defaultFilterChain"]["filters"][0]["typedConfig"]
    manager.pop("accessLog", None)
    manager.pop("rds")
    manager["routeConfig"] = route_config
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
def _envoy(connector_docs: list[dict]) -> Iterator[int]:
    with tempfile.TemporaryDirectory() as raw:
        directory = Path(raw)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        config = directory / "envoy.yaml"
        config.write_text(
            yaml.safe_dump(_static_config(_translate(connector_docs, directory), port))
        )
        envoy = _bin("ENVOY_BIN", "envoy")
        check = subprocess.run(
            [envoy, "--mode", "validate", "-c", str(config)],
            capture_output=True,
            check=False,
            text=True,
        )
        if check.returncode != 0:
            raise RuntimeError(f"Envoy rejected translated config: {check.stderr}")
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


def _get(
    port: int,
    identity: str | None = None,
    *,
    path: str = _PATH,
    worker: str | None = None,
) -> int:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.putrequest("GET", path, skip_host=True)
    connection.putheader("Host", _HOST)
    if identity is not None:
        connection.putheader("CF-Connecting-IP", identity)
    if worker is not None:
        connection.putheader("CF-Worker", worker)
    connection.endheaders()
    response = connection.getresponse()
    response.read()
    connection.close()
    return response.status


def _assistant_pattern() -> re.Pattern[str]:
    values = yaml.safe_load(_DEPLOY_VALUES.read_text())
    chart = yaml.safe_load((_CHART_DIR / "values.yaml").read_text())
    bucket = {
        **chart["route"]["assistantBucket"],
        **values.get("route", {}).get("assistantBucket", {}),
    }
    # RE2's inline (?i:...) group is also Python syntax, so the chart's pattern
    # compiles unchanged.
    return re.compile(bucket["pattern"])


def test_assistant_pattern_covers_exactly_the_metered_ranges() -> None:
    prefixes = re.findall(
        r'netip\.MustParsePrefix\("([^"]+)"\)', _METER_SOURCE.read_text()
    )
    assert prefixes, "no prefixes found in internal/meter"
    pattern = _assistant_pattern()
    for prefix in prefixes:
        network = ipaddress.ip_network(prefix)
        inside = [
            network[0],
            network[1],
            network[network.num_addresses // 2],
            network[-1],
        ]
        if network.version == 6:
            # A non-zero third hextet still inside a /48 only exists when the
            # prefix is shorter, so probe deep interface bits instead.
            inside.append(network[0] + (1 << 64) + 5)
        for address in inside:
            assert pattern.fullmatch(address.compressed), address.compressed
            assert pattern.fullmatch(address.compressed.upper()), address.compressed
        below, above = network[0] - 1, network[-1] + 1
        for address in (below, above):
            assert not pattern.fullmatch(address.compressed), address.compressed


def test_ordinary_clients_keep_their_own_buckets() -> None:
    with _envoy(_render_connector()) as port:
        caller = "203.0.113.10"
        assert [_get(port, caller) for _ in range(120)] == [200] * 120
        assert _get(port, caller) == 429
        # A neighbour of an assistant range is an ordinary caller.
        assert [_get(port, "160.79.106.1") for _ in range(120)] == [200] * 120
        assert _get(port, "160.79.106.1") == 429
        assert _get(port, "2607:6bc1::1") == 200
        # Missing and malformed identities share the fail-safe bucket.
        assert [_get(port) for _ in range(120)] == [200] * 120
        assert _get(port, "malformed") == 429
        # Workers cannot spoof an identity.
        assert _get(port, "192.0.2.40", worker="jomcgi.dev") == 403
        assert _get(port, _CROSS_ZONE_WORKER_IP, worker="other.example") == 403


def test_assistant_ranges_share_one_large_bucket_and_skip_per_client() -> None:
    with _envoy(_render_connector()) as port:
        # One assistant address well past the per-client 120 still passes:
        # it draws only from the shared bucket.
        single = "160.79.104.17"
        assert [_get(port, single) for _ in range(200)] == [200] * 200
        assert _get(port, "2607:6bc0::1") == 200
        assert _get(port, "2607:6bc0:0:2::9") == 200
        # Exhausting a per-client bucket leaves assistants untouched.
        caller = "198.51.100.7"
        for _ in range(120):
            _get(port, caller)
        assert _get(port, caller) == 429
        assert _get(port, "160.79.105.250") == 200


def test_assistant_bucket_is_shared_across_the_ranges() -> None:
    accelerated = (
        "route.assistantBucket.requests=5",
        "route.assistantBucket.unit=Minute",
    )
    with _envoy(_render_connector(*accelerated)) as port:
        addresses = [
            "160.79.104.1",
            "160.79.105.2",
            "2607:6bc0::3",
            "160.79.104.200",
            "2607:6bc0:0:9::1",
        ]
        assert [_get(port, address) for address in addresses] == [200] * 5
        # The sixth assistant call, from yet another address, is refused:
        # one bucket for all of them.
        assert _get(port, "160.79.105.99") == 429
        # Ordinary callers are unaffected by the exhausted shared bucket.
        assert _get(port, "203.0.113.55") == 200
