"""Fill authentik's generated secrets once and copy the OIDC client secrets.

authentik-secrets (authentik namespace) is the source of truth. Each key is
generated only when it is absent, so reruns never rotate a value. The OIDC
client secrets are then copied to the namespaces that present them: the edge
Gateway's SecurityPolicy, ArgoCD and itsaplan (factory chart).

Uses only the standard library and the pod's service account token.
"""

import base64
import json
import os
import secrets
import ssl
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

SOURCE = ("authentik", "authentik-secrets")
GENERATED = {
    # Django signing key; changing it invalidates every session and token.
    "AUTHENTIK_SECRET_KEY": 60,
    "EDGE_OIDC_CLIENT_SECRET": 48,
    "ARGOCD_OIDC_CLIENT_SECRET": 48,
    "ITSAPLAN_OIDC_CLIENT_SECRET": 48,
}
# (namespace, name, key in the copy, key in the source, seconds to wait for
# it). ArgoCD creates each Secret empty; this fills it. The factory chart's
# Secret comes from another Application, which may sync after this one.
COPIES = [
    ("edge", "edge-oidc-client", "client-secret", "EDGE_OIDC_CLIENT_SECRET", 0),
    (
        "argocd",
        "argocd-authentik-oidc",
        "client-secret",
        "ARGOCD_OIDC_CLIENT_SECRET",
        0,
    ),
    (
        "factory",
        "factory-itsaplan-oidc",
        "client-secret",
        "ITSAPLAN_OIDC_CLIENT_SECRET",
        900,
    ),
]


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
        # 403: the Role granting this name is not there yet (it comes with the
        # Secret's Application), the same as a missing Secret.
        if e.code in (403, 404) and method == "GET":
            return None
        raise SystemExit(f"{method} {path}: {e.code} {e.read().decode()}")


def secret_path(namespace, name=""):
    return f"/api/v1/namespaces/{namespace}/secrets" + (f"/{name}" if name else "")


def b64(value):
    return base64.b64encode(value.encode()).decode()


def main():
    namespace, name = SOURCE
    source = call("GET", secret_path(namespace, name))
    if source is None:
        raise SystemExit(f"{namespace}/{name} is missing; ArgoCD creates it")
    data = dict(source.get("data") or {})
    missing = [key for key in GENERATED if key not in data]
    for key in missing:
        data[key] = b64(secrets.token_urlsafe(GENERATED[key]))
    if missing:
        source["data"] = data
        call("PUT", secret_path(namespace, name), source)
        print(f"added {', '.join(missing)} to {namespace}/{name}")
    else:
        print(f"{namespace}/{name} complete")

    for copy_ns, copy_name, copy_key, source_key, wait in COPIES:
        want = {copy_key: data[source_key]}
        deadline = time.monotonic() + wait
        current = call("GET", secret_path(copy_ns, copy_name))
        while current is None and time.monotonic() < deadline:
            print(f"waiting for {copy_ns}/{copy_name}")
            time.sleep(10)
            current = call("GET", secret_path(copy_ns, copy_name))
        if current is None:
            raise SystemExit(f"{copy_ns}/{copy_name} is missing; ArgoCD creates it")
        if current.get("data") != want:
            current["data"] = want
            call("PUT", secret_path(copy_ns, copy_name), current)
            print(f"updated {copy_ns}/{copy_name}")
        else:
            print(f"{copy_ns}/{copy_name} current")


if __name__ == "__main__":
    main()
