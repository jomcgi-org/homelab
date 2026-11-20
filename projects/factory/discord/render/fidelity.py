"""Run against the renderer with python3 fidelity.py [base URL]."""

from http.client import HTTPConnection
from pathlib import Path
import sys
import unittest
from urllib.parse import urlsplit

BASE = urlsplit(sys.argv.pop(1) if len(sys.argv) > 1 else "http://localhost:8080")
GOLDENS = Path(__file__).with_name("goldens")


def post(route, body):
    connection = HTTPConnection(BASE.hostname, BASE.port, timeout=10)
    try:
        connection.request("POST", route, body)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


class Renderer(unittest.TestCase):
    def test_goldens(self):
        for name in ("bar", "flowchart", "sequence", "class", "state", "gantt", "pie"):
            png = GOLDENS / f"{name}.png"
            with self.subTest(png=png.name):
                vega = png.stem == "bar"
                spec = png.with_suffix(".json" if vega else ".mmd").read_bytes()
                status, result = post("/vega-lite" if vega else "/mermaid", spec)
                self.assertEqual(status, 200, result[:80])
                self.assertTrue(result == png.read_bytes(), f"{png.name} changed")

    def test_refusals(self):
        cases = [
            (
                "/vega-lite",
                b'{"layer":[{"data":{"url":"https://example.org/data.csv"}}]}',
                422,
            ),
            ("/mermaid", b'flowchart LR\nA-->B\nclick B "https://example.org"', 422),
            ("/mermaid", b"sequenceDiagram\nlink Alice: Dashboard @ /local", 422),
            ("/vega-lite", b" " * 65537, 413),
            ("/mermaid", b" " * 65537, 413),
            ("/vega-lite", b'{"width":4097,"height":10,"mark":"point"}', 422),
        ]
        for route, body, expected in cases:
            with self.subTest(route=route, size=len(body)):
                self.assertEqual(post(route, body)[0], expected)


if __name__ == "__main__":
    unittest.main()
