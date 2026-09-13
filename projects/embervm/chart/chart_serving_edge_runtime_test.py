"""Runtime contracts for cold and warmed cluster-serving edge Envoy."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

_CHART_DIR = Path(__file__).resolve().parent
_NODE_ID = "embervm-serving-edge"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _render(
    data_port: int, admin_port: int, health_port: int, grpc_port: int
) -> list[dict]:
    argv = [
        os.environ["HELM_BIN"],
        "template",
        "edge-runtime",
        str(_CHART_DIR),
        "--namespace",
        "ember-test",
        "--set",
        f"servingEnvoy.listenerPort={data_port}",
        "--set",
        f"servingEnvoy.edge.adminPort={admin_port}",
        "--set",
        f"servingEnvoy.edge.healthPort={health_port}",
        "--set",
        f"xds.grpcPort={grpc_port}",
    ]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"helm template failed: {result.stderr}")
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def _component(doc: dict) -> str | None:
    return doc.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component")


def _edge_resources(docs: list[dict]) -> tuple[dict, dict, dict]:
    deployment = next(
        doc
        for doc in docs
        if doc.get("kind") == "Deployment" and _component(doc) == "serving-edge"
    )
    config_map = next(
        doc
        for doc in docs
        if doc.get("kind") == "ConfigMap" and _component(doc) == "serving-edge"
    )
    bootstrap = yaml.safe_load(config_map["data"]["envoy-bootstrap.yaml"])
    health_bootstrap = yaml.safe_load(config_map["data"]["envoy-health-proxy.yaml"])
    return deployment, bootstrap, health_bootstrap


def _status(url: str, timeout: float = 1.0) -> int:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (TimeoutError, urllib.error.URLError):
        return 0


def _wait_for_status(url: str, wanted: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last = 0
    while time.monotonic() < deadline:
        last = _status(url)
        if last == wanted:
            return
        time.sleep(0.1)
    raise AssertionError(f"{url} returned {last}, wanted {wanted}")


def _request_body(url: str, host: str) -> bytes:
    request = urllib.request.Request(url, headers={"Host": host})
    with urllib.request.urlopen(request, timeout=2.0) as response:
        assert response.status == 200
        return response.read()


def _wait_for_body(url: str, host: str, wanted: bytes) -> None:
    deadline = time.monotonic() + 15.0
    last: bytes | str = b""
    while time.monotonic() < deadline:
        try:
            last = _request_body(url, host)
            if last == wanted:
                return
        except (TimeoutError, urllib.error.URLError) as error:
            last = str(error)
        time.sleep(0.1)
    raise AssertionError(f"route returned {last!r}, wanted {wanted!r}")


def _stop(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


class _UpstreamHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b"edge-runtime-ok\n"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def test_cold_edge_survives_startup_budget_and_warmed_edge_survives_ads_loss(
    tmp_path: Path,
) -> None:
    grpc_port = _free_port()
    snapshot_port = _free_port()
    data_port = _free_port()
    admin_port = _free_port()
    health_port = _free_port()

    docs = _render(data_port, admin_port, health_port, grpc_port)
    deployment, bootstrap, health_bootstrap = _edge_resources(docs)
    container = next(
        item
        for item in deployment["spec"]["template"]["spec"]["containers"]
        if item["name"] == "envoy"
    )
    startup_probe = container["startupProbe"]
    startup_budget = (
        startup_probe["periodSeconds"] * startup_probe["failureThreshold"]
    )
    assert startup_budget == 60

    xds_cluster = next(
        cluster
        for cluster in bootstrap["static_resources"]["clusters"]
        if cluster["name"] == "xds_cluster"
    )
    socket_address = xds_cluster["load_assignment"]["endpoints"][0]["lb_endpoints"][
        0
    ]["endpoint"]["address"]["socket_address"]
    socket_address["address"] = "127.0.0.1"

    bootstrap_path = tmp_path / "envoy-bootstrap.yaml"
    bootstrap_path.write_text(yaml.safe_dump(bootstrap), encoding="utf-8")
    health_bootstrap_path = tmp_path / "envoy-health-proxy.yaml"
    health_bootstrap_path.write_text(
        yaml.safe_dump(health_bootstrap), encoding="utf-8"
    )
    xds_log = tmp_path / "xds.log"
    envoy_log = tmp_path / "envoy.log"
    health_log = tmp_path / "health-proxy.log"

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()

    xds_process: subprocess.Popen[bytes] | None = None
    envoy_process: subprocess.Popen[bytes] | None = None
    health_process: subprocess.Popen[bytes] | None = None
    with (
        xds_log.open("wb") as xds_output,
        envoy_log.open("wb") as envoy_output,
        health_log.open("wb") as health_output,
    ):
        try:
            xds_env = os.environ.copy()
            xds_env.update(
                {
                    "EMBERVM_XDS_GRPC_PORT": str(grpc_port),
                    "EMBERVM_XDS_HTTP_PORT": str(snapshot_port),
                }
            )
            xds_process = subprocess.Popen(
                [os.environ["XDS_BIN"]],
                env=xds_env,
                stdout=xds_output,
                stderr=subprocess.STDOUT,
            )
            _wait_for_status(f"http://127.0.0.1:{snapshot_port}/healthz", 200)

            envoy_process = subprocess.Popen(
                [
                    os.environ["ENVOY_BIN"],
                    "--config-path",
                    str(bootstrap_path),
                    "--service-node",
                    _NODE_ID,
                    "--service-cluster",
                    "edge-runtime",
                    "--concurrency",
                    "1",
                    "--disable-hot-restart",
                ],
                stdout=envoy_output,
                stderr=subprocess.STDOUT,
            )
            health_process = subprocess.Popen(
                [
                    os.environ["ENVOY_BIN"],
                    "--config-path",
                    str(health_bootstrap_path),
                    "--service-node",
                    f"{_NODE_ID}-health",
                    "--service-cluster",
                    "edge-runtime-health",
                    "--concurrency",
                    "1",
                    "--disable-hot-restart",
                ],
                stdout=health_output,
                stderr=subprocess.STDOUT,
            )

            server_info = f"http://127.0.0.1:{health_port}/server_info"
            readiness = f"http://127.0.0.1:{health_port}/ready"
            _wait_for_status(server_info, 200)
            _wait_for_status(readiness, 503)
            assert _status(f"http://127.0.0.1:{health_port}/config_dump") == 403
            cold_pid = envoy_process.pid
            health_pid = health_process.pid

            deadline = time.monotonic() + startup_budget + 1
            while time.monotonic() < deadline:
                assert _status(server_info) == 200
                assert _status(readiness) == 503
                assert envoy_process.poll() is None
                assert health_process.poll() is None
                time.sleep(startup_probe["periodSeconds"])

            assert envoy_process.pid == cold_pid
            assert health_process.pid == health_pid
            assert (
                _status(f"http://127.0.0.1:{snapshot_port}/snapshot/{_NODE_ID}")
                == 404
            )

            snapshot = {
                "version": "0000000001",
                "clusters": [
                    {
                        "name": "runtime-upstream",
                        "connect_timeout_ms": 200,
                        "endpoints": [
                            {
                                "ip": "127.0.0.1",
                                "port": upstream.server_address[1],
                            }
                        ],
                    }
                ],
                "routes": [
                    {
                        "host": "runtime.test",
                        "path_prefix": "/",
                        "cluster": "runtime-upstream",
                        "request_headers": {},
                    }
                ],
            }
            request = urllib.request.Request(
                f"http://127.0.0.1:{snapshot_port}/snapshot/{_NODE_ID}",
                data=json.dumps(snapshot).encode(),
                method="PUT",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=5.0) as response:
                assert response.status == 200

            _wait_for_status(readiness, 200)
            data_url = f"http://127.0.0.1:{data_port}/runtime"
            _wait_for_body(data_url, "runtime.test", b"edge-runtime-ok\n")

            _stop(xds_process)
            assert xds_process.poll() is not None
            time.sleep(1)
            assert _status(readiness) == 200
            assert _request_body(data_url, "runtime.test") == b"edge-runtime-ok\n"
            assert envoy_process.poll() is None
            assert health_process.poll() is None
        finally:
            _stop(health_process)
            _stop(envoy_process)
            _stop(xds_process)
            upstream.shutdown()
            upstream.server_close()
            upstream_thread.join(timeout=5)
