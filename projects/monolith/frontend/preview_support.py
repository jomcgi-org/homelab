"""File hashing and loopback serving for offline static preview runners."""

import functools
import hashlib
import http.server
import threading
from contextlib import contextmanager
from urllib.parse import urlsplit


def digest_tree(root):
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@contextmanager
def static_server(root, *, prefix="/", handler=http.server.SimpleHTTPRequestHandler):
    """Serve a directory until exit, yielding its loopback base URL.

    A non-root prefix preserves the preview runners' GET-only rewriting and
    rejects requests outside that exact prefix, including its trailing slash.
    A custom handler may supply additional behavior for root serving.
    """
    if not prefix.startswith("/") or not prefix.endswith("/"):
        raise ValueError("prefix must start and end with a slash")

    if prefix != "/":

        class PrefixedHandler(handler):
            def do_GET(self):
                path = urlsplit(self.path).path
                if not path.startswith(prefix):
                    self.send_error(403)
                    return
                self.path = "/" + path.removeprefix(prefix)
                super().do_GET()

            def log_message(self, *_args):
                pass

        handler = PrefixedHandler

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(handler, directory=str(root))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    try:
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}{prefix}"
        finally:
            server.shutdown()
    finally:
        server.server_close()
        if thread.ident is not None:
            thread.join()
