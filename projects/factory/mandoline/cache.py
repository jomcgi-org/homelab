"""Temporary storage of complete Mandoline evidence for identical inputs."""

import hashlib
import json
import os
import tempfile
from pathlib import Path


def cache_key(*, tree, configs, tool_digest, pack_digests, invocation):
    """Use Git's tree ID and content digests supplied by the trusted caller."""
    inputs = [1, tree, configs, tool_digest, pack_digests, invocation]
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class Cache:
    def __init__(self, checkout):
        parent = Path(os.environ.get("TMPDIR") or tempfile.gettempdir()).resolve()
        self.directory = parent / f"mandoline-{os.getuid()}"
        if self.directory.is_relative_to(checkout.resolve()):
            raise ValueError("temporary cache would be inside checkout")
        self.directory.mkdir(mode=0o700, exist_ok=True)
        info = self.directory.lstat()
        if (
            self.directory.is_symlink()
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("temporary cache must be a private directory")

    def path(self, key):
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("invalid cache digest")
        return self.directory / key

    def read(self, key):
        try:
            return json.loads(self.path(key).read_bytes())
        except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    def store(self, key, envelope):
        if envelope["status"] != "complete":
            return
        destination = self.path(key)
        with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as output:
            temporary = Path(output.name)
            try:
                output.write(json.dumps(envelope).encode())
                output.flush()
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
