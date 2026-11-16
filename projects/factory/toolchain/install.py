"""Install repository-pinned tools into an already bootstrapped Alpine root."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request


def install_binary(name: str, pin: dict, root: Path) -> None:
    archive = root / "download"
    with urllib.request.urlopen(pin["url"], timeout=120) as response:
        with archive.open("wb") as dest:
            shutil.copyfileobj(response, dest)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != pin["sha256"]:
        raise ValueError(f"checksum mismatch for {name}")
    output = root / "bin" / name
    if pin["kind"] == "file":
        archive.replace(output)
    else:
        # Read only the pinned executable, never extract arbitrary archive paths.
        with tarfile.open(archive) as bundle:
            member = bundle.getmember(pin["file"])
            if not member.isfile():
                raise ValueError(f"{name} is not a regular archive member")
            with bundle.extractfile(member) as source, output.open("wb") as dest:
                shutil.copyfileobj(source, dest)
        archive.unlink()
    output.chmod(0o755)


def wrapper(root: Path, name: str, command: str) -> None:
    output = root / "bin" / name
    output.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        'root="$(cd "$(dirname "$0")/.." && pwd)"\n'
        f'exec {command} "$@"\n'
    )
    output.chmod(0o755)


def install(repo: Path, root: Path) -> None:
    locks = json.loads((repo / "bazel/tools/tools.lock.json").read_text())
    for name in ("helm", "crane", "buildifier", "gofumpt", "shfmt", "shellcheck"):
        pin = next(
            b
            for b in locks[name]["binaries"]
            if b["os"] == "linux" and b["cpu"] == "x86_64"
        )
        install_binary(name, pin, root)

    # The tools OCI image's GNU libc Node/Python/Ruff binaries cannot run in
    # this musl harness. APK supplies Node/Python; PyPI supplies Ruff's musl
    # wheel at exactly the version in the repository's tool lock.
    ruff_url = locks["ruff"]["binaries"][0]["url"]
    ruff_version = re.search(r"/download/([^/]+)/", ruff_url).group(1)
    subprocess.run(
        [
            str(root / "bin/python3"),
            "-m",
            "pip",
            "--isolated",
            "install",
            "--disable-pip-version-check",
            "--only-binary=:all:",
            "--no-deps",
            "--index-url",
            "https://pypi.org/simple",
            "--target",
            str(root / "python"),
            f"ruff=={ruff_version}",
        ],
        check=True,
    )
    wrapper(root, "ruff", '"$root/python/bin/ruff"')

    package = json.loads((repo / "package.json").read_text())
    dependencies = package["devDependencies"]
    packages = [package["packageManager"]] + [
        f"{name}@{dependencies[name]}"
        for name in ("prettier", "prettier-plugin-svelte", "svelte")
    ]
    # npm requires separate paths for user and global configuration.
    npm_global_config = root / "npm-global.conf"
    npm_global_config.write_text("")
    subprocess.run(
        [
            str(root / "bin/node"),
            str(root / "runtime/usr/lib/node_modules/npm/bin/npm-cli.js"),
            "install",
            "--prefix",
            str(root / "js"),
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            "--userconfig=/dev/null",
            f"--globalconfig={npm_global_config}",
            "--registry=https://registry.npmjs.org",
            *packages,
        ],
        check=True,
    )
    wrapper(root, "pnpm", '"$root/bin/node" "$root/js/node_modules/pnpm/bin/pnpm.cjs"')
    wrapper(
        root,
        "prettier",
        'env NODE_PATH="$root/js/node_modules" "$root/bin/node" '
        '"$root/js/node_modules/prettier/bin/prettier.cjs"',
    )


if __name__ == "__main__":
    install(Path(sys.argv[1]), Path(sys.argv[2]))
