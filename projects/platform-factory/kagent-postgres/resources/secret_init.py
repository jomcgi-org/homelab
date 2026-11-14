"""Generate kagent's database passwords once and write its connection strings.

kagent-pg-logins (kagent-pg namespace) is the source of truth. Each password is
generated only when it is absent, so reruns never rotate one; kagent-pg-setup
then sets them on the database with kagent's setup script. The connection
strings the kagent chart reads (kagent-postgres-owner and
kagent-postgres-readwrite, in the kagent namespace) are derived from it.

Uses only the standard library and the pod's service account token.
"""

import base64
import json
import os
import secrets
import ssl
import urllib.error
import urllib.request

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
API = "https://{}:{}".format(
    os.environ["KUBERNETES_SERVICE_HOST"], os.environ["KUBERNETES_SERVICE_PORT"]
)
with open(f"{SA}/token") as f:
    TOKEN = f.read()
CTX = ssl.create_default_context(cafile=f"{SA}/ca.crt")

NAMESPACE = "kagent-pg"
SOURCE = "kagent-pg-logins"
TARGET_NAMESPACE = "kagent"
HOST = "kagent-pg-rw.kagent-pg.svc"
DATABASE = "kagent"
# (password key in SOURCE, login, Secret the chart reads, key in it)
LOGINS = [
    (
        "owner-password",
        "kagent_owner_user",
        "kagent-postgres-owner",
        "ownerConnectionString",
    ),
    (
        "readwrite-password",
        "kagent_readwrite_user",
        "kagent-postgres-readwrite",
        "readWriteConnectionString",
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
        if e.code == 404 and method == "GET":
            return None
        raise SystemExit(f"{method} {path}: {e.code} {e.read().decode()}")


def secret_path(name, namespace=NAMESPACE):
    return f"/api/v1/namespaces/{namespace}/secrets/{name}"


def get(name, namespace=NAMESPACE):
    secret = call("GET", secret_path(name, namespace))
    if secret is None:
        raise SystemExit(f"{namespace}/{name} is missing; ArgoCD creates it")
    return secret


def b64(value):
    return base64.b64encode(value.encode()).decode()


def main():
    source = get(SOURCE)
    data = dict(source.get("data") or {})
    # Hex, so the password needs no escaping in a connection URL.
    missing = [key for key, _, _, _ in LOGINS if key not in data]
    for key in missing:
        data[key] = b64(secrets.token_hex(24))
    if missing:
        source["data"] = data
        call("PUT", secret_path(SOURCE), source)
        print(f"added {', '.join(missing)} to {NAMESPACE}/{SOURCE}")
    else:
        print(f"{NAMESPACE}/{SOURCE} complete")

    for key, login, name, url_key in LOGINS:
        password = base64.b64decode(data[key]).decode()
        url = f"postgres://{login}:{password}@{HOST}:5432/{DATABASE}?sslmode=require"
        want = {url_key: b64(url)}
        current = get(name, TARGET_NAMESPACE)
        if current.get("data") != want:
            current["data"] = want
            call("PUT", secret_path(name, TARGET_NAMESPACE), current)
            print(f"updated {TARGET_NAMESPACE}/{name}")
        else:
            print(f"{TARGET_NAMESPACE}/{name} current")


if __name__ == "__main__":
    main()
