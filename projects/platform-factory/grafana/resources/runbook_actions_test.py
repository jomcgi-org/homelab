"""Exercise the alert page as an operator opening a reviewed action."""

import importlib.util
from html.parser import HTMLParser
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread


class Forms(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []

    def handle_starttag(self, tag, attrs):
        if tag == "form":
            self.forms.append(dict(attrs))


def test_reviewed_actions_on_alert_pages(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "runbooks", Path(__file__).parent / "runbooks/server.py"
    )
    app = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(app)
    available = {"runbook-kagent-revert.grafana.svc"}
    monkeypatch.setattr(app, "service_available", available.__contains__)
    monkeypatch.setattr(app, "query", lambda *args, **kwargs: [])
    with app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:

            def forms(name):
                conn = HTTPConnection(*server.server_address, timeout=2)
                try:
                    conn.request("GET", f"/{name}?action=kagent-revert&pod=unreviewed")
                    response = conn.getresponse()
                    assert response.status == 200
                    parser = Forms()
                    parser.feed(response.read().decode())
                    return parser.forms
                finally:
                    conn.close()

            assert forms("KagentSuspendTimedOut") == [
                {"method": "get", "action": "/actions/kagent-revert"}
            ]
            assert forms("NodeDiskFilling") == []
            available.clear()
            assert forms("KagentSuspendTimedOut") == []
        finally:
            server.shutdown()
            thread.join()
