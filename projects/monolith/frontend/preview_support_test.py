"""Real loopback and filesystem checks for static preview support."""

import hashlib
import http.server
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

import preview_support


class DigestTreeTests(unittest.TestCase):
    def test_relative_sorted_maps_across_roots_and_changes(self):
        with (
            tempfile.TemporaryDirectory() as first,
            tempfile.TemporaryDirectory() as second,
        ):
            roots = [Path(first), Path(second)]
            for root in roots:
                (root / "nested").mkdir()
                (root / "nested/z.txt").write_bytes(b"z")
                (root / "a.txt").write_bytes(b"a")
                (root / "empty").mkdir()
            expected = {
                name: hashlib.sha256(content).hexdigest()
                for name, content in [("a.txt", b"a"), ("nested/z.txt", b"z")]
            }
            for root in roots:
                self.assertEqual(preview_support.digest_tree(root), expected)
                self.assertEqual(
                    list(preview_support.digest_tree(root)), list(expected)
                )
            root = roots[0]
            (root / "a.txt").write_bytes(b"changed")
            changed = preview_support.digest_tree(root)
            self.assertNotEqual(changed["a.txt"], expected["a.txt"])
            self.assertEqual(changed["nested/z.txt"], expected["nested/z.txt"])
            (root / "added").write_bytes(b"new")
            self.assertEqual(
                set(preview_support.digest_tree(root)), {*expected, "added"}
            )
            (root / "a.txt").unlink()
            self.assertEqual(
                set(preview_support.digest_tree(root)), {"nested/z.txt", "added"}
            )

    def test_empty_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(preview_support.digest_tree(Path(directory)), {})


class StaticServerTests(unittest.TestCase):
    def request(self, url):
        with urlopen(url, timeout=5) as response:
            return response.read()

    def test_prefixed_get_queries_and_rejections(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.html").write_bytes(b"index")
            (root / "asset.js").write_bytes(b"asset")
            with preview_support.static_server(root, prefix="/nested/preview/") as base:
                self.assertEqual(self.request(base), b"index")
                self.assertEqual(self.request(base + "asset.js?version=1"), b"asset")
                origin = base.removesuffix("/nested/preview/")
                for path in (
                    "/asset.js",
                    "/nested/preview",
                    "/nested/preview?x=1",
                    "/nested/preview-other/",
                ):
                    with self.subTest(path=path), self.assertRaises(HTTPError) as error:
                        self.request(origin + path)
                    self.assertEqual(error.exception.code, 403)

    def test_root_and_custom_handler(self):
        class Handler(http.server.SimpleHTTPRequestHandler):
            def end_headers(self):
                self.send_header("X-Preview", "custom")
                super().end_headers()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.html").write_bytes(b"root")
            (root / "asset.js").write_bytes(b"asset")
            with preview_support.static_server(root, handler=Handler) as base:
                self.assertEqual(self.request(base), b"root")
                with urlopen(base + "asset.js?x=1", timeout=5) as response:
                    self.assertEqual(response.read(), b"asset")
                    self.assertEqual(response.headers["X-Preview"], "custom")

    def test_normal_and_exception_exit_close_socket_and_join_thread(self):
        real_server = http.server.ThreadingHTTPServer
        real_thread = threading.Thread
        for fail in (False, True):
            servers, threads = [], []

            def server(*args, **kwargs):
                instance = real_server(*args, **kwargs)
                servers.append(instance)
                return instance

            def thread(*args, **kwargs):
                instance = real_thread(*args, **kwargs)
                threads.append(instance)
                return instance

            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                (Path(directory) / "index.html").write_bytes(b"served")
                with (
                    patch.object(
                        preview_support.http.server, "ThreadingHTTPServer", server
                    ),
                    patch.object(preview_support.threading, "Thread", thread),
                ):
                    try:
                        with preview_support.static_server(Path(directory)) as base:
                            self.assertEqual(self.request(base), b"served")
                            self.assertEqual(servers[0].server_address[0], "127.0.0.1")
                            address = servers[0].server_address
                            if fail:
                                raise RuntimeError("browser failed")
                    except RuntimeError as error:
                        self.assertTrue(fail)
                        self.assertEqual(str(error), "browser failed")
                    else:
                        self.assertFalse(fail)
                self.assertTrue(threads)
                self.assertTrue(all(not thread.is_alive() for thread in threads))
                self.assertEqual(servers[0].socket.fileno(), -1)
                with socket.socket() as connection:
                    connection.settimeout(1)
                    self.assertNotEqual(connection.connect_ex(address), 0)


if __name__ == "__main__":
    unittest.main()
