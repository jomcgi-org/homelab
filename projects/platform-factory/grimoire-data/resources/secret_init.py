"""Generate the Grimoire runtime logins once and wait for the backup key.

The grimoire-db-init Job runs this before the Cluster. It fills each login
Secret's password when it is absent, so a rerun never rotates one; the
operator sets each managed role's password from its Secret. Then it waits for
garage-init to fill grimoire-backup-s3: an instance that starts without the
key archives nothing until it restarts.

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
NAMESPACE = "grimoire"
BACKUP_KEY = "grimoire-backup-s3"
# (login Secret, managed role)
LOGINS = [
    ("grimoire-api-login", "grimoire_api"),
    ("grimoire-worker-login", "grimoire_worker"),
]


def call(method, path, body=None):
    api = "https://{}:{}".format(
        os.environ["KUBERNETES_SERVICE_HOST"], os.environ["KUBERNETES_SERVICE_PORT"]
    )
    with open(f"{SA}/token") as f:
        token = f.read()
    req = urllib.request.Request(
        api + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    context = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    try:
        with urllib.request.urlopen(req, context=context, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path}: {e.code} {e.read().decode()}")


def secret_path(name):
    return f"/api/v1/namespaces/{NAMESPACE}/secrets/{name}"


def b64(value):
    return base64.b64encode(value.encode()).decode()


def unb64(value):
    return base64.b64decode(value).decode()


def ensure_logins():
    for name, login in LOGINS:
        source = call("GET", secret_path(name))
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


def wait_for_key(sleep=time.sleep):
    while "AWS_SECRET_ACCESS_KEY" not in (
        call("GET", secret_path(BACKUP_KEY)).get("data") or {}
    ):
        print(
            f"waiting for garage-init (CronJob garage/garage-bootstrap, every "
            f"10 minutes) to fill {NAMESPACE}/{BACKUP_KEY}"
        )
        sleep(15)
    print(f"{NAMESPACE}/{BACKUP_KEY} has a key")


if __name__ == "__main__":
    ensure_logins()
    wait_for_key()
