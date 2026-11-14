"""Generate itsaplan's secrets once (the factory chart's itsaplan-secret-init).

The chart declares each Secret empty and the Application ignores its data, so
this Job needs only get and update on those names. A value is generated only
when it is absent, so reruns never rotate one:

  ITSAPLAN_SECRET  BETTER_AUTH_SECRET and APP_ENCRYPTION_KEY. Neither may ever
                   change: the first signs every session and the second
                   encrypts the stored sign-in and mail credentials.
  ADMIN_SECRET     The break-glass admin's email (ADMIN_EMAIL, written once)
                   and password.

ITSAPLAN_SECRET is generated only for an empty database. When the database
already holds accounts or settings (DB_ROWS, counted read-only by the Job's
db-state init container) and either key is missing, the Secret was lost:
new keys would end every session and leave the stored credentials
unreadable, so this refuses. Restore the old values, or accept the loss by
annotating the Secret with ALLOW_ANNOTATION=true (see the factory-cluster
README, "itsaplan"); the annotation is removed when the keys are written.

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
NAMESPACE = os.environ["NAMESPACE"]
ALLOW_ANNOTATION = "factory.jomcgi.dev/regenerate-itsaplan-keys"

GENERATED = {
    os.environ["ITSAPLAN_SECRET"]: {
        # 32 random bytes, base64, as upstream's `openssl rand -base64 32`.
        "BETTER_AUTH_SECRET": lambda: base64.b64encode(secrets.token_bytes(32)),
        "APP_ENCRYPTION_KEY": lambda: base64.b64encode(secrets.token_bytes(32)),
    },
    os.environ["ADMIN_SECRET"]: {
        "email": lambda: os.environ["ADMIN_EMAIL"].encode(),
        "password": lambda: secrets.token_urlsafe(32).encode(),
    },
}


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
        raise SystemExit(f"{method} {path}: {e.code} {e.read().decode()}")


def database_rows():
    """Accounts and settings rows in itsaplan's database (0 before migrations)."""
    with open(os.environ["DB_ROWS"]) as f:
        return int(f.read().strip() or "0")


def main():
    rows = database_rows()
    print(f"itsaplan's database holds {rows} account and settings rows")
    for name, keys in GENERATED.items():
        path = f"/api/v1/namespaces/{NAMESPACE}/secrets/{name}"
        secret = call("GET", path)
        data = dict(secret.get("data") or {})
        missing = [key for key in keys if not data.get(key)]
        annotations = secret["metadata"].get("annotations") or {}
        allowed = annotations.get(ALLOW_ANNOTATION) == "true"
        if missing and name == os.environ["ITSAPLAN_SECRET"] and rows and not allowed:
            raise SystemExit(
                f"REFUSING to generate {', '.join(missing)} in {NAMESPACE}/{name}: "
                f"the database already holds {rows} account and settings rows, so "
                "these keys existed and were lost. New ones would end every session "
                "and make the stored OIDC, mail and SCIM credentials unreadable. "
                "Restore the old values into the Secret, or accept the loss with "
                f"`kubectl -n {NAMESPACE} annotate secret {name} "
                f"{ALLOW_ANNOTATION}=true` and sync again (factory-cluster README, "
                '"itsaplan").'
            )
        for key in missing:
            data[key] = base64.b64encode(keys[key]()).decode()
        if missing:
            secret["data"] = data
            if allowed:
                annotations.pop(ALLOW_ANNOTATION)
                secret["metadata"]["annotations"] = annotations
            call("PUT", path, secret)
            print(f"added {', '.join(missing)} to {NAMESPACE}/{name}")
        else:
            print(f"{NAMESPACE}/{name} complete")


if __name__ == "__main__":
    main()
