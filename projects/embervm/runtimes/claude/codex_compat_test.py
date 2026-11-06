"""Exercise the pinned Codex binary against a credential-free local backend."""

import http.server
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import shim


class Backend(http.server.ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), BackendHandler)
        self.requests = []
        self.responses = 0


class BackendHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def record(self, body=None):
        request = {
            "method": self.command,
            "path": self.path,
            "placeholder_auth": self.headers.get("Authorization")
            == "Bearer " + shim.CodexProcess._dummy_jwt(),
            "placeholder_account": self.headers.get("chatgpt-account-id")
            == shim.CODEX_DUMMY_ACCOUNT_ID,
            "body": body,
        }
        self.server.requests.append(request)
        return request

    def send(self, status, data, content_type="application/json"):
        payload = data.encode() if isinstance(data, str) else json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_CONNECT(self):
        # All proxy traffic is closed here. Tests never contact a provider.
        self.record()
        self.send(403, {"error": "external traffic is disabled"})

    def do_GET(self):
        request = self.record()
        path = urlsplit(self.path)
        if path.netloc or path.path != "/backend-api/codex/models":
            self.send(404, {"error": "unsupported local endpoint"})
        elif not request["placeholder_auth"] or not request["placeholder_account"]:
            self.send(403, {"error": "placeholder auth and account are required"})
        else:
            self.send(200, {"models": []})

    def do_POST(self):
        data = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.headers.get("Content-Encoding"):
            self.record()
            self.send(400, {"error": "test transport expects uncompressed JSON"})
            return
        body = json.loads(data) if data else {}
        request = self.record(body)
        if self.path != "/backend-api/codex/responses":
            self.send(404, {"error": "unsupported local endpoint"})
            return
        if not request["placeholder_auth"] or not request["placeholder_account"]:
            self.send(403, {"error": "placeholder auth and account are required"})
            return
        self.server.responses += 1
        turn = self.server.responses
        events = [
            {"type": "response.created", "response": {"id": f"response-{turn}"}},
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "id": f"message-{turn}",
                    "content": [
                        {"type": "output_text", "text": f"Local reply {turn}."}
                    ],
                },
            },
            {
                "type": "response.completed",
                "response": {
                    "id": f"response-{turn}",
                    "usage": {
                        "input_tokens": 10 * turn,
                        "input_tokens_details": {"cached_tokens": 2 * turn},
                        "output_tokens": 3 * turn,
                        "output_tokens_details": {"reasoning_tokens": turn},
                        "total_tokens": 13 * turn,
                    },
                },
            },
        ]
        self.send(
            200,
            "".join(
                "event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n"
                for event in events
            ),
            "text/event-stream",
        )


class LocalBackendProcess(shim.CodexProcess):
    def __init__(self, *args, catalog_only=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.catalog_only = catalog_only

    def _write_model_config(self, codex_home):
        result = super()._write_model_config(codex_home)
        path = Path(codex_home) / "config.toml"
        config = path.read_text()
        if self.catalog_only:
            # Discovery needs no filesystem tools. Narrow its permissions so
            # this test also runs under a managed host's read-only policy.
            config = config.replace(
                'sandbox_mode = "danger-full-access"', 'sandbox_mode = "read-only"'
            )
        # This stdlib test server reads JSON, not zstd. Auth selection,
        # request paths, and the app-server protocol stay unchanged.
        config += "\n[features]\nenable_request_compression = false\n"
        shim._write_read_only_file(str(path), config)
        return result


class CodexCompatibilityTest(unittest.TestCase):
    def setUp(self):
        binary = os.environ.get("CODEX_COMPAT_BINARY")
        if not binary:
            self.skipTest("requires the pinned @codex_cli binary")
        binary = Path(binary)
        if not binary.is_absolute() and "TEST_SRCDIR" in os.environ:
            binary = Path(os.environ["TEST_SRCDIR"]) / "_main" / binary
        self.binary = str(binary.resolve(strict=True))
        self.backend = Backend()
        self.addCleanup(self.backend.server_close)
        thread = threading.Thread(target=self.backend.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.backend.shutdown)
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        env = {
            shim.CODEX_SUBSCRIPTION_BASE_URL_ENV: (
                f"http://127.0.0.1:{self.backend.server_port}/backend-api/"
            ),
            shim.EGRESS_PORT_ENV: str(self.backend.server_port),
            shim.AGENT_MCP_URL_ENV: "",
            "OPENAI_API_KEY": "must-not-reach-codex",
            "CODEX_API_KEY": "must-not-reach-codex",
        }
        self.enterContext(patch.dict(os.environ, env))
        self.enterContext(patch.object(shim, "_cli_privilege_kwargs", return_value={}))
        self.enterContext(patch.object(shim, "TURN_READ_TIMEOUT", 20))

    def process(self, **kwargs):
        manager = LocalBackendProcess(
            workspace=self.workspace.name, executable=self.binary, **kwargs
        )
        self.addCleanup(manager._close_process, kill=True)
        return manager

    def test_catalog_carries_placeholder_subscription_auth(self):
        manager = self.process(catalog_only=True)
        manager._spawn()
        manager._request("model/list", {"includeHidden": True}, timeout=20)
        requests = [
            request
            for request in self.backend.requests
            if urlsplit(request["path"]).path == "/backend-api/codex/models"
        ]
        self.assertTrue(requests)
        for request in requests:
            self.assertTrue(request["placeholder_auth"])
            self.assertTrue(request["placeholder_account"])
        self.assertFalse(
            any("unrecognized configuration" in line for line in manager.stderr_lines)
        )

    def test_terra_xhigh_turn_and_persisted_resume(self):
        manager = self.process()
        first = manager.turn("Reply once.", model="terra", effort="xhigh")
        session_id = first["session_id"]
        self.assertTrue(session_id)
        manager._close_process(kill=False)
        second = manager.turn(
            "Reply again.", session_id=session_id, model="terra", effort="xhigh"
        )
        for turn, record in enumerate((first, second), 1):
            self.assertEqual(record["session_id"], session_id)
            self.assertEqual(record["provider_model"], "gpt-6.1-terra")
            self.assertEqual(record["effort"], "xhigh")
            self.assertEqual(record["result"], f"Local reply {turn}.")
            self.assertEqual(record["terminal_reason"], "completed")
            self.assertEqual(record["usage"]["input_tokens"], 10 * turn)
            self.assertEqual(record["usage"]["cache_read_tokens"], 2 * turn)
            self.assertEqual(record["usage"]["output_tokens"], 3 * turn)
        requests = [
            request
            for request in self.backend.requests
            if request["path"] == "/backend-api/codex/responses"
        ]
        self.assertEqual(len(requests), 2)
        for request in requests:
            self.assertTrue(request["placeholder_auth"])
            self.assertTrue(request["placeholder_account"])
            self.assertEqual(request["body"]["model"], "gpt-6.1-terra")
            self.assertEqual(request["body"]["reasoning"]["effort"], "xhigh")


if __name__ == "__main__":
    unittest.main()
