#!/usr/bin/env python3
"""Refresh non-GitHub tool URLs and SHA256 together, without executing downloads.

Only fixed upstream origins and known lock shapes are accepted. Each invocation
updates one tool family across all platforms, or writes nothing on any failure.
Registry metadata and HTTPS transport supply discovery, not a safety approval.
"""

import argparse
import base64
import hashlib
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOOLS = {"atlas", "helm", "op", "claude"}
SEMVER = re.compile(r"v?(\d+)\.(\d+)\.(\d+)\Z")
ORIGINS = {
    "api.github.com",
    "release.ariga.io",
    "get.helm.sh",
    "app-updates.agilebits.com",
    "cache.agilebits.com",
    "registry.npmjs.org",
}
LIMIT = 256 * 1024 * 1024


def allowed(url):
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in ORIGINS
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ValueError("unexpected upstream origin")


class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        allowed(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, limit=2 * 1024 * 1024):
    allowed(url)
    # No repository credential is attached to upstream downloads.
    with urllib.request.build_opener(Redirect).open(
        urllib.request.Request(
            url, headers={"User-Agent": "homelab-dependency-maintenance"}
        ),
        timeout=60,
    ) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("upstream response exceeds bound")
    return data


def version(value):
    match = SEMVER.fullmatch(value)
    if not match:
        raise ValueError("expected stable semantic version")
    return tuple(int(part) for part in match.groups())


def old_enough(value, now):
    published = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return published <= now - timedelta(days=3)


def choose(rows, now):
    candidates = [
        value
        for value, date in rows
        if SEMVER.fullmatch(value) and old_enough(date, now)
    ]
    if not candidates:
        raise ValueError("no stable release with a verified age of at least three days")
    return max(candidates, key=version).removeprefix("v")


def latest(tool, now, read):
    if tool in ("atlas", "helm"):
        project = "ariga/atlas" if tool == "atlas" else "helm/helm"
        rows = json.loads(
            read(f"https://api.github.com/repos/{project}/releases?per_page=20")
        )
        return choose(
            [
                (r["tag_name"], r["published_at"])
                for r in rows
                if not r["draft"] and not r["prerelease"]
            ],
            now,
        )
    if tool == "op":
        history = read(
            "https://app-updates.agilebits.com/product_history/CLI2"
        ).decode()
        rows = re.findall(
            r"<h3>\s*(\d+\.\d+\.\d+)\s*<span[^>]*>.*?released (\d{4}-\d{2}-\d{2})",
            history,
            re.S,
        )
        return choose(rows, now)
    metadata = json.loads(read("https://registry.npmjs.org/@anthropic-ai/claude-code"))
    return choose(list(metadata["time"].items()), now)


def replace_binary(tool, binary, new, read):
    old_url = binary["url"]
    patterns = {
        "atlas": r"https://release\.ariga\.io/atlas/atlas-community-(?:linux|darwin)-(?:arm64|amd64)-v(\d+\.\d+\.\d+)",
        "helm": r"https://get\.helm\.sh/helm-v(\d+\.\d+\.\d+)-(?:linux|darwin)-(?:arm64|amd64)\.tar\.gz",
        "op": r"https://cache\.agilebits\.com/dist/1P/op2/pkg/v(\d+\.\d+\.\d+)/op_(?:linux|darwin)_(?:arm64|amd64)_v\1\.zip",
        "claude": r"https://registry\.npmjs\.org/@anthropic-ai/claude-code-(?:linux|darwin)-(?:arm64|x64)/-/claude-code-(?:linux|darwin)-(?:arm64|x64)-(\d+\.\d+\.\d+)\.tgz",
    }
    match = re.fullmatch(patterns[tool], old_url)
    if not match or not re.fullmatch(r"[0-9a-f]{64}", binary["sha256"]):
        raise ValueError("unsupported lock shape or checksum")
    old = match[1]
    if version(new) <= version(old):
        return dict(binary)
    new_url = old_url.replace(old, new)
    expected = None
    if tool in ("atlas", "helm"):
        suffix = ".sha256" if tool == "atlas" else ".sha256sum"
        expected = read(new_url + suffix).decode().split()[0]
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("invalid publisher checksum")
    integrity = None
    if tool == "claude":
        package = old_url.split("/-/")[0].removeprefix("https://registry.npmjs.org/")
        metadata = json.loads(read(f"https://registry.npmjs.org/{package}/{new}"))
        if (
            metadata["name"] != package
            or metadata["version"] != new
            or metadata["dist"]["tarball"] != new_url
        ):
            raise ValueError("npm package identity mismatch")
        integrity = metadata["dist"]["integrity"]
        if not re.fullmatch(r"sha512-[A-Za-z0-9+/]+={0,2}", integrity):
            raise ValueError("npm SHA512 integrity missing")
    data = read(new_url, LIMIT)
    digest = hashlib.sha256(data).hexdigest()
    if expected and expected != digest:
        raise ValueError("publisher checksum mismatch")
    if (
        integrity
        and integrity
        != "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
    ):
        raise ValueError("npm integrity mismatch")
    # op's official HTTPS archives have no separate checksum endpoint. This
    # records the downloaded bytes, as Renovate's release-attachment fallback
    # does. Independent review still verifies publisher provenance/signatures.
    return {**binary, "url": new_url, "sha256": digest}


def update(lock, tool, *, now=None, read=fetch):
    if tool not in TOOLS:
        raise ValueError("unsupported tool")
    now = now or datetime.now(timezone.utc)
    new = latest(tool, now, read)
    binaries = lock[tool]["binaries"]
    if not binaries:
        raise ValueError("empty platform inventory")
    result = json.loads(json.dumps(lock))
    result[tool]["binaries"] = [replace_binary(tool, b, new, read) for b in binaries]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", choices=sorted(TOOLS))
    parser.add_argument(
        "--lock", type=Path, default=Path("bazel/tools/tools.lock.json")
    )
    args = parser.parse_args()
    original = json.loads(args.lock.read_text())
    result = update(original, args.tool)
    if result != original:
        args.lock.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
