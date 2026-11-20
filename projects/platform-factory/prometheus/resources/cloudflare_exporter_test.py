"""cloudflare-exporter exports the egress IP and each credential's check.

Every address is from the RFC 5737 documentation ranges, and every credential
is a fake.
"""

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
    "cloudflare_exporter", Path(__file__).resolve().parent / "cloudflare_exporter.py"
)
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)

TRACE = (
    "fl=1f\nh=api.cloudflare.com\nip=192.0.2.10\nts=1795000000.1\nvisit_scheme=https\n"
)


@pytest.fixture(autouse=True)
def empty_state(monkeypatch):
    monkeypatch.setattr(exporter, "egress", {})
    monkeypatch.setattr(exporter, "credentials", {})
    monkeypatch.setattr(exporter, "reasons", {})


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


def fail(error):
    def raise_it():
        raise error

    return raise_it


def test_a_trace_read_exports_the_egress_ip(monkeypatch):
    monkeypatch.setattr(exporter.time, "time", lambda: 123)
    exporter.update(lambda: exporter.parse_trace(TRACE), {})
    text = exporter.render()
    check_metrics(text)
    assert values(text) == {
        "factory_egress_ip_read_success": "1",
        'factory_egress_ip_info{ip="192.0.2.10"}': "1",
        'factory_egress_ip_observed_timestamp_seconds{ip="192.0.2.10"}': "123",
    }


def test_a_failed_trace_read_keeps_the_last_ip_and_logs_no_address(capsys, monkeypatch):
    monkeypatch.setattr(exporter.time, "time", lambda: 123)
    exporter.update(lambda: exporter.parse_trace(TRACE), {})
    monkeypatch.setattr(exporter.time, "time", lambda: 456)
    for page in [
        "fl=1f\nh=api.cloudflare.com\n",
        "ip=not-an-address\n",
        "ip=2001:db8::1\n",
    ]:
        exporter.update(lambda p=page: exporter.parse_trace(p), {})
        text = exporter.render()
        check_metrics(text)
        assert values(text) == {
            "factory_egress_ip_read_success": "0",
            'factory_egress_ip_info{ip="192.0.2.10"}': "1",
            'factory_egress_ip_observed_timestamp_seconds{ip="192.0.2.10"}': "123",
        }
    exporter.update(fail(OSError("connection refused")), {})
    assert values(exporter.render())["factory_egress_ip_read_success"] == "0"
    err = capsys.readouterr().err
    assert "trace: read failed: ValueError" in err
    assert "trace: read failed: OSError" in err
    assert "192.0.2" not in err


def test_a_failed_trace_read_before_any_valid_read_exports_only_read_success_0():
    exporter.update(fail(OSError("timed out")), {})
    text = exporter.render()
    check_metrics(text)
    assert values(text) == {"factory_egress_ip_read_success": "0"}


def test_a_new_address_replaces_the_old_one(monkeypatch):
    monkeypatch.setattr(exporter.time, "time", lambda: 123)
    exporter.update(lambda: "192.0.2.10", {})
    monkeypatch.setattr(exporter.time, "time", lambda: 456)
    exporter.update(lambda: "198.51.100.20", {})
    assert values(exporter.render()) == {
        "factory_egress_ip_read_success": "1",
        'factory_egress_ip_info{ip="198.51.100.20"}': "1",
        'factory_egress_ip_observed_timestamp_seconds{ip="198.51.100.20"}': "456",
    }


def test_2xx_accepts_and_401_or_403_refuses_a_credential():
    exporter.update(None, {"a": lambda: 200, "b": lambda: 401, "c": lambda: 403})
    text = exporter.render()
    check_metrics(text)
    assert values(text) == {
        'factory_cloudflare_credential_check_success{credential="a"}': "1",
        'factory_cloudflare_credential_check_success{credential="b"}': "1",
        'factory_cloudflare_credential_check_success{credential="c"}': "1",
        'factory_cloudflare_credential_auth_success{credential="a"}': "1",
        'factory_cloudflare_credential_auth_success{credential="b"}': "0",
        'factory_cloudflare_credential_auth_success{credential="c"}': "0",
    }


def test_a_check_without_an_answer_keeps_the_last_answer(capsys):
    exporter.update(None, {"api": lambda: 403})
    for check in [fail(OSError("timed out")), lambda: 500, lambda: 429]:
        exporter.update(None, {"api": check})
        text = exporter.render()
        check_metrics(text)
        assert values(text) == {
            'factory_cloudflare_credential_check_success{credential="api"}': "0",
            'factory_cloudflare_credential_auth_success{credential="api"}': "0",
        }
    exporter.update(None, {"api": lambda: 200})
    assert values(exporter.render()) == {
        'factory_cloudflare_credential_check_success{credential="api"}': "1",
        'factory_cloudflare_credential_auth_success{credential="api"}': "1",
    }
    assert capsys.readouterr().err.splitlines() == [
        "api: refused with HTTP 403",
        "api: check failed: OSError",
        "api: answered with HTTP 500",
        "api: answered with HTTP 429",
        "api: accepted with HTTP 200",
    ]


def test_a_check_before_any_answer_exports_only_check_success_0():
    exporter.update(None, {"api": fail(exporter.NoCredential())})
    text = exporter.render()
    check_metrics(text)
    assert values(text) == {
        'factory_cloudflare_credential_check_success{credential="api"}': "0"
    }


def test_a_result_is_logged_once_until_it_changes(capsys):
    for _ in range(3):
        exporter.update(lambda: "192.0.2.10", {"api": lambda: 200})
    assert capsys.readouterr().err.splitlines() == [
        "trace: read",
        "api: accepted with HTTP 200",
    ]


def test_credential_labels_are_escaped():
    exporter.update(None, {'a"b\\c\nd': lambda: 200})
    text = exporter.render()
    check_metrics(text)
    assert (
        'factory_cloudflare_credential_auth_success{credential="a\\"b\\\\c\\nd"} 1'
        in text
    )


class Recorder(http.server.BaseHTTPRequestHandler):
    """Answer every GET with the server's status and record the request."""

    def do_GET(self):
        # Header names are case-insensitive.
        headers = {k.lower(): v for k, v in self.headers.items()}
        self.server.requests.append((self.path, headers))
        self.send_response(self.server.status)
        self.end_headers()
        self.wfile.write(b"ip=192.0.2.10\n" if self.server.status == 200 else b"{}")

    def log_message(self, *args):
        pass


@pytest.fixture
def upstream():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Recorder)
    server.requests = []
    server.status = 200
    server.base = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


def test_the_api_check_sends_the_token_as_a_bearer_token(upstream):
    url = upstream.base + "/client/v4/accounts/abc/cfd_tunnel?per_page=1"
    for status in [200, 401, 403]:
        upstream.status = status
        assert exporter.api_status(url, "fake-token") == status
    path, headers = upstream.requests[0]
    assert path == "/client/v4/accounts/abc/cfd_tunnel?per_page=1"
    assert headers["authorization"] == "Bearer fake-token"
    with pytest.raises(exporter.NoCredential):
        exporter.api_status(url, "")
    assert len(upstream.requests) == 3


def test_the_trace_read_needs_http_200(upstream):
    assert exporter.read_trace(upstream.base + "/cdn-cgi/trace") == "192.0.2.10"
    upstream.status = 503
    with pytest.raises(ValueError):
        exporter.read_trace(upstream.base + "/cdn-cgi/trace")


def test_the_environment_selects_the_checks(upstream):
    trace, checks = exporter.from_env({})
    assert (trace, checks) == (None, {})
    env = {
        "TRACE_URL": upstream.base + "/cdn-cgi/trace",
        "API_CREDENTIAL": "factory-health-cloudflare",
        "API_URL": upstream.base
        + "/client/v4/accounts/abc/workers/scripts/jomcgi-site/deployments",
    }
    trace, checks = exporter.from_env(env)
    assert trace() == "192.0.2.10"
    assert list(checks) == ["factory-health-cloudflare"]
    # The Secret is optional: an empty token is no credential, not a refusal.
    with pytest.raises(exporter.NoCredential):
        checks["factory-health-cloudflare"]()
    env["CLOUDFLARE_API_TOKEN"] = "fake-token"
    assert checks["factory-health-cloudflare"]() == 200


def test_the_server_answers_metrics_only():
    exporter.update(lambda: "192.0.2.10", {})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), exporter.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urlopen(base + "/metrics") as response:
            assert response.status == 200
            assert b'factory_egress_ip_info{ip="192.0.2.10"} 1' in response.read()
        try:
            urlopen(base + "/")
            raise AssertionError("/ answered")
        except HTTPError as error:
            assert error.code == 404
    finally:
        server.shutdown()
