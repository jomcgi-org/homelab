"""Download the Semgrep Pro engine and the Secrets rules. The only step that uses the token.

    SEMGREP_APP_TOKEN=... python3 -I fetch-pro.py OUT_DIR

Writes OUT_DIR/semgrep-core-proprietary, OUT_DIR/rules.json and OUT_DIR/fetch.json
(sizes and sha256 of both files, no token). The calls are the ones of the Semgrep CLI
of the pinned version (`semgrep install-semgrep-pro` and `semgrep ci --config secrets`
with SEMGREP_REPO_NAME unset), made with the standard library:

  GET {SEMGREP_URL}/api/agent/deployments/deepbinary/{PRO_PLATFORM}?version={V}
      the engine binary
  GET {SEMGREP_URL}/api/agent/deployments/scans/config
          ?is_secrets_scan=true&dry_run=true&full_scan=true&semgrep_version={V}
      the Secrets rules of the deployment; the body holds "rules" or "rule_config"
      (a JSON string); the file is rules.json with the key "rules" only, sorted by id

The token goes only to https://semgrep.dev (the host is fixed in this file,
not in pins.env), only in the Authorization header, over https, and is dropped when a
redirect leaves the host. Nothing here prints it, and an error shows only the class of
the error and the HTTP status. The token never goes to a file, a log or an artifact.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHUNK = 1 << 20
MAX_BINARY_BYTES = 2 << 30
MAX_RULES_BYTES = 256 << 20


def read_pins() -> dict[str, str]:
    pins: dict[str, str] = {}
    for path in (HERE.parent / "engine" / "pins.env", HERE / "pins.env"):
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                key, _, value = line.partition("=")
                pins[key] = value
    return pins


TOKEN_HOST = "semgrep.dev"


def check_token_host(url: str) -> None:
    """The token goes to semgrep.dev and nowhere else. The host is fixed here, not
    read from pins.env: a change of a pins file alone must not send the token to
    another host. Port, user info and a non-https scheme are refused too."""
    parts = urllib.parse.urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname != TOKEN_HOST
        or parts.port is not None
        or parts.username is not None
        or parts.password is not None
    ):
        sys.exit(
            f"fetch-pro: the token goes only to https://{TOKEN_HOST}, not to this host"
        )


class KeepAuthOnSameHost(urllib.request.HTTPRedirectHandler):
    """urllib copies the request headers to a redirect target, whatever the host. This
    handler sends the token only to the host it was meant for."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is None:
            return None
        old_host = urllib.parse.urlsplit(req.full_url).netloc
        target = urllib.parse.urlsplit(newurl)
        new.headers.pop("Authorization", None)
        if target.scheme == "https" and target.netloc == old_host:
            new.add_unredirected_header(
                "Authorization", req.get_header("Authorization") or ""
            )
        return new


def open_url(url: str, token: str, accept: str):
    if not url.startswith("https://"):
        sys.exit("fetch-pro: refusing a URL that is not https")
    check_token_host(url)
    request = urllib.request.Request(
        url, headers={"Accept": accept, "User-Agent": "substrate-fork-engine-pro"}
    )
    request.add_unredirected_header("Authorization", f"Bearer {token}")
    opener = urllib.request.build_opener(KeepAuthOnSameHost)
    return opener.open(request, timeout=180)


def get(url: str, token: str, accept: str, what: str):
    try:
        return open_url(url, token, accept)
    except urllib.error.HTTPError as err:
        hint = {
            401: "the token is not valid",
            403: "the deployment has no access to the Pro engine",
        }.get(err.code, "")
        sys.exit(f"fetch-pro: {what}: HTTP {err.code} {hint}".rstrip())
    except (
        Exception
    ) as err:  # the message of a network error can name the URL, not the token
        sys.exit(f"fetch-pro: {what}: {type(err).__name__}")


def download_binary(base: str, platform: str, version: str, token: str, out: Path):
    url = (
        f"{base}/api/agent/deployments/deepbinary/{platform}"
        f"?{urllib.parse.urlencode({'version': version})}"
    )
    digest, size = hashlib.sha256(), 0
    with get(url, token, "application/octet-stream", "engine download") as resp:
        tmp = out.with_suffix(".part")
        with tmp.open("wb") as fh:
            while chunk := resp.read(CHUNK):
                size += len(chunk)
                if size > MAX_BINARY_BYTES:
                    sys.exit("fetch-pro: the engine download is over 2 GiB")
                digest.update(chunk)
                fh.write(chunk)
    tmp.chmod(0o755)
    tmp.replace(out)
    return digest.hexdigest(), size


def download_rules(base: str, version: str, token: str, out: Path):
    query = urllib.parse.urlencode(
        {
            "is_secrets_scan": "true",
            "dry_run": "true",
            "full_scan": "true",
            "semgrep_version": version,
        }
    )
    url = f"{base}/api/agent/deployments/scans/config?{query}"
    with get(url, token, "application/json", "rules download") as resp:
        raw = resp.read(MAX_RULES_BYTES + 1)
    if len(raw) > MAX_RULES_BYTES:
        sys.exit("fetch-pro: the rules download is over 256 MiB")
    try:
        doc = json.loads(raw)
        config = doc if "rules" in doc else doc["rule_config"]
        if isinstance(config, str):
            config = json.loads(config)
        rules = config["rules"]
    except (ValueError, KeyError, TypeError):
        sys.exit("fetch-pro: the rules answer has neither rules nor rule_config")
    if not isinstance(rules, list) or not all(
        isinstance(r, dict) and isinstance(r.get("id"), str) for r in rules
    ):
        sys.exit("fetch-pro: the rules answer is not a list of rules with an id")
    rules.sort(key=lambda r: r["id"])
    body = json.dumps({"rules": rules}, sort_keys=True, separators=(",", ":")).encode()
    out.write_bytes(body)
    return hashlib.sha256(body).hexdigest(), len(rules)


def main(out_dir: str) -> None:
    token = os.environ.get("SEMGREP_APP_TOKEN", "")
    if not token:
        sys.exit("fetch-pro: SEMGREP_APP_TOKEN is empty or not set")
    if token != token.strip() or len(token) < 16 or any(c.isspace() for c in token):
        sys.exit("fetch-pro: SEMGREP_APP_TOKEN does not look like a token")
    pins = read_pins()
    # Before any request: the pinned URL must name the one host that may see the token.
    check_token_host(pins["SEMGREP_URL"])
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    version = pins["SEMGREP_VERSION"]
    binary_sha, binary_bytes = download_binary(
        pins["SEMGREP_URL"],
        pins["PRO_PLATFORM"],
        version,
        token,
        out / "semgrep-core-proprietary",
    )
    if pins.get("PRO_BINARY_SHA256") and binary_sha != pins["PRO_BINARY_SHA256"]:
        sys.exit(
            f"fetch-pro: the engine sha256 is {binary_sha}, not the pinned {pins['PRO_BINARY_SHA256']}"
        )
    if binary_bytes < int(pins["PRO_BINARY_MIN_BYTES"]):
        sys.exit(f"fetch-pro: the engine is {binary_bytes} bytes, too small")
    with (out / "semgrep-core-proprietary").open("rb") as fh:
        if fh.read(4) != b"\x7fELF":
            sys.exit("fetch-pro: the engine download is not an ELF binary")
    rules_sha, rule_count = download_rules(
        pins["SEMGREP_URL"], version, token, out / "rules.json"
    )
    if rule_count < int(pins["RULES_MIN_COUNT"]):
        sys.exit(
            f"fetch-pro: only {rule_count} rules, want {pins['RULES_MIN_COUNT']} or more"
        )
    info = {
        "semgrep_version": version,
        "platform": pins["PRO_PLATFORM"],
        "binary_sha256": binary_sha,
        "binary_bytes": binary_bytes,
        "rules_sha256": rules_sha,
        "rules_count": rule_count,
    }
    (out / "fetch.json").write_text(json.dumps(info, indent=2, sort_keys=True) + "\n")
    print(
        f"fetched engine {binary_bytes} bytes sha256:{binary_sha[:12]} "
        f"and {rule_count} rules sha256:{rules_sha[:12]}"
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
