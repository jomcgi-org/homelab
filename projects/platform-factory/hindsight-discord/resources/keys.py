"""Generate the Discord tenant key once and copy it only to the bridge.

init fills hindsight-discord/hindsight-keys before the Deployment starts.
discord copies tenant-api-key to factory-discord/hindsight-discord-key after
sync. Reruns preserve the source key and repair a stale consumer copy.
Uses only the standard library and the pod's service account token.
"""

import base64
import json
import os
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.request

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
API = "https://{}:{}".format(
    os.environ["KUBERNETES_SERVICE_HOST"], os.environ["KUBERNETES_SERVICE_PORT"]
)
with open(f"{SA}/token") as f:
    TOKEN = f.read()
CTX = ssl.create_default_context(cafile=f"{SA}/ca.crt")

NAMESPACE = "hindsight-discord"
KEYS = "hindsight-keys"
FIELDS = ("tenant-api-key",)
CONSUMER = ("factory-discord", "hindsight-discord-key")


def call(method, path, body=None):
    req = urllib.request.Request(
        API + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        # 403: the Role naming this Secret is not there yet (it comes from
        # another Application), the same as a missing Secret.
        if e.code in (403, 404) and method == "GET":
            return None
        raise SystemExit(f"{method} {path}: {e.code}")


def secret_path(name, namespace):
    return f"/api/v1/namespaces/{namespace}/secrets/{name}"


def get_eventually(name, namespace, timeout=900):
    """Wait for a Secret ArgoCD creates, for up to timeout seconds."""
    deadline = time.monotonic() + timeout
    while True:
        secret = call("GET", secret_path(name, namespace))
        if secret is not None:
            return secret
        if time.monotonic() > deadline:
            raise SystemExit(f"{namespace}/{name} or its Role is still missing")
        print(f"waiting for {namespace}/{name}")
        time.sleep(10)


def b64(value):
    return base64.b64encode(value.encode()).decode()


def ensure_keys():
    """Fill each absent key once; return the Secret's data."""
    source = get_eventually(KEYS, NAMESPACE)
    data = dict(source.get("data") or {})
    missing = [field for field in FIELDS if not data.get(field)]
    for field in missing:
        # Hex, so the value needs no escaping in a header.
        data[field] = b64(secrets.token_hex(32))
    if missing:
        source["data"] = data
        call("PUT", secret_path(KEYS, NAMESPACE), source)
        print(f"filled {NAMESPACE}/{KEYS}: {', '.join(missing)}")
    else:
        print(f"{NAMESPACE}/{KEYS} complete")
    return data


def share_key(data):
    want = {"tenant-api-key": data["tenant-api-key"]}
    namespace, name = CONSUMER
    current = get_eventually(name, namespace)
    if current.get("data") != want:
        current["data"] = want
        call("PUT", secret_path(name, namespace), current)
        print(f"updated {namespace}/{name}")
    else:
        print(f"{namespace}/{name} current")


def main(mode):
    if mode not in ("init", "discord"):
        raise SystemExit(f"unknown mode {mode!r}; use init or discord")
    data = ensure_keys()
    if mode == "discord":
        share_key(data)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
