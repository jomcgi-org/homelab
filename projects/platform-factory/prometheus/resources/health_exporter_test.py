"""health-exporter turns the public health.json into valid Prometheus gauges."""

import http.server
import importlib.util
import os
from pathlib import Path
import subprocess
import threading
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

SPEC = importlib.util.spec_from_file_location(
    "health_exporter", Path(__file__).resolve().parent / "health_exporter.py"
)
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)

HEALTHY = {
    "status": "healthy",
    "generatedAt": "2026-11-19T21:10:03Z",
    "components": [
        {"name": "factory-cluster", "state": "up"},
        {"name": "jomcgi-site-worker", "state": "up"},
    ],
}
HEALTHY_VALUES = {
    "factory_health_read_success": "1",
    "factory_health_healthy": "1",
    "factory_health_generated_at_seconds": "1795122603",
    'factory_health_component_up{component="factory-cluster"}': "1",
    'factory_health_component_up{component="jomcgi-site-worker"}': "1",
}
UNHEALTHY = dict(
    HEALTHY,
    status="unhealthy",
    components=HEALTHY["components"] + [{"name": "alert-test", "state": "down"}],
)


@pytest.fixture(autouse=True)
def no_valid_read(monkeypatch):
    monkeypatch.setattr(exporter, "last_valid", [])


def unreachable():
    raise OSError("connection refused")


def check_metrics(text):
    # promtool's linter rejects malformed exposition text.
    subprocess.run(
        [os.environ["PROMTOOL"], "check", "metrics"],
        input=text,
        text=True,
        check=True,
    )


def values(text):
    return dict(
        line.rsplit(" ", 1) for line in text.splitlines() if not line.startswith("#")
    )


def test_a_healthy_file_exports_status_age_and_components():
    text = exporter.render(lambda: HEALTHY)
    check_metrics(text)
    assert values(text) == HEALTHY_VALUES


def test_an_unhealthy_file_exports_its_down_component():
    text = exporter.render(lambda: UNHEALTHY)
    check_metrics(text)
    assert values(text)["factory_health_healthy"] == "0"
    assert values(text)['factory_health_component_up{component="alert-test"}'] == "0"


def test_a_failed_read_keeps_the_series_of_the_last_valid_read(capsys):
    valid = values(exporter.render(lambda: UNHEALTHY))
    for fetch in [unreachable, lambda: {}]:
        text = exporter.render(fetch)
        check_metrics(text)
        assert values(text) == dict(valid, factory_health_read_success="0")
    assert "health.json read failed: OSError" in capsys.readouterr().err


def test_a_valid_read_drops_a_removed_component():
    exporter.render(lambda: UNHEALTHY)
    exporter.render(unreachable)
    text = exporter.render(lambda: HEALTHY)
    check_metrics(text)
    assert values(text) == HEALTHY_VALUES


def test_a_failed_read_before_any_valid_read_exports_only_read_success_0(capsys):
    invalid = [
        {},
        dict(HEALTHY, generatedAt="2026-11-19T21:10:03"),
        dict(HEALTHY, generatedAt="yesterday"),
        dict(HEALTHY, components=[{"state": "up"}]),
    ]
    for fetch in [unreachable] + [lambda d=d: d for d in invalid]:
        text = exporter.render(fetch)
        check_metrics(text)
        assert values(text) == {"factory_health_read_success": "0"}
    assert "health.json read failed: OSError" in capsys.readouterr().err


def test_component_labels_are_escaped():
    document = dict(HEALTHY, components=[{"name": 'a"b\\c\nd', "state": "up"}])
    text = exporter.render(lambda: document)
    check_metrics(text)
    assert 'factory_health_component_up{component="a\\"b\\\\c\\nd"} 1' in text


def test_the_server_answers_metrics_only(monkeypatch):
    monkeypatch.setattr(exporter, "read", lambda: HEALTHY)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), exporter.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urlopen(base + "/metrics") as response:
            assert response.status == 200
            assert b"factory_health_healthy 1" in response.read()
        try:
            urlopen(base + "/")
            raise AssertionError("/ answered")
        except HTTPError as error:
            assert error.code == 404
    finally:
        server.shutdown()
