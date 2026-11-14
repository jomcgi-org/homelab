"""Generate factory-db's login passwords once and write its connection strings.

Two modes, both safe to rerun:

  init         Before the Cluster: fill each login Secret's password when it
               is absent, then wait for garage-init to fill the backup key.
  connections  After every sync: write each database's connection string
               into the factory namespace.

The login Secrets (factory-db namespace) are the source of truth: the operator
sets each role's password from its Secret, and a rerun never rotates one. The
connection strings the factory chart reads are derived from them.

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

NAMESPACE = "factory-db"
S3 = "factory-db-s3"
TARGET_NAMESPACE = "factory"
HOST = "factory-db-rw.factory-db.svc"
# (login Secret, database, connection-string Secret in TARGET_NAMESPACE)
LOGINS = [
    ("factory-db-itsaplan-login", "itsaplan", "factory-itsaplan-db"),
    ("factory-db-connector-login", "factory_connector", "factory-connector-db"),
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
        # 403: the Role granting this name is not there yet (it comes from
        # another Application), the same as a missing Secret.
        if e.code in (403, 404) and method == "GET":
            return None
        raise SystemExit(f"{method} {path}: {e.code} {e.read().decode()}")


def secret_path(name, namespace=NAMESPACE):
    return f"/api/v1/namespaces/{namespace}/secrets/{name}"


def get(name, namespace=NAMESPACE):
    secret = call("GET", secret_path(name, namespace))
    if secret is None:
        raise SystemExit(f"{namespace}/{name} is missing; ArgoCD creates it")
    return secret


def get_eventually(name, namespace, timeout=900):
    """Wait for a Secret another Application creates, for up to timeout s."""
    deadline = time.monotonic() + timeout
    while True:
        secret = call("GET", secret_path(name, namespace))
        if secret is not None:
            return secret
        if time.monotonic() > deadline:
            raise SystemExit(
                f"{namespace}/{name} or its Role is still missing; "
                "the factory-db-secrets Application creates them"
            )
        print(f"waiting for {namespace}/{name} (factory-db-secrets)")
        time.sleep(10)


def b64(value):
    return base64.b64encode(value.encode()).decode()


def unb64(value):
    return base64.b64decode(value).decode()


def ensure_logins():
    """Fill each login Secret's password once; return {Secret: password}."""
    passwords = {}
    for name, login, _ in LOGINS:
        source = get(name)
        data = dict(source.get("data") or {})
        changed = False
        if unb64(data.get("username", "")) != login:
            data["username"] = b64(login)
            changed = True
        if not data.get("password"):
            # Hex, so the password needs no escaping in a connection URL.
            data["password"] = b64(secrets.token_hex(24))
            changed = True
        if changed:
            source["data"] = data
            call("PUT", secret_path(name), source)
            print(f"filled {NAMESPACE}/{name}")
        else:
            print(f"{NAMESPACE}/{name} complete")
        passwords[name] = unb64(data["password"])
    return passwords


def wait_for_key():
    """Return once garage-init has written factory-db's Garage key."""
    while True:
        data = get(S3).get("data") or {}
        if "AWS_SECRET_ACCESS_KEY" in data:
            print(f"{NAMESPACE}/{S3} has a key")
            return
        print(
            f"waiting for garage-init (CronJob garage/garage-bootstrap, every "
            f"10 minutes) to fill {NAMESPACE}/{S3}"
        )
        time.sleep(15)


def write_connections(passwords):
    for name, login, target in LOGINS:
        password = passwords[name]
        url = f"postgres://{login}:{password}@{HOST}:5432/{login}?sslmode=require"
        want = {"DATABASE_URL": b64(url)}
        current = get_eventually(target, TARGET_NAMESPACE)
        if current.get("data") != want:
            current["data"] = want
            call("PUT", secret_path(target, TARGET_NAMESPACE), current)
            print(f"updated {TARGET_NAMESPACE}/{target}")
        else:
            print(f"{TARGET_NAMESPACE}/{target} current")


def main(mode):
    passwords = ensure_logins()
    if mode == "init":
        wait_for_key()
    elif mode == "connections":
        write_connections(passwords)
    else:
        raise SystemExit(f"unknown mode {mode!r}; use init or connections")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
