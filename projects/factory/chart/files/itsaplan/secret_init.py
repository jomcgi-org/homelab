"""Generate itsaplan's secrets once (the factory chart's itsaplan-secret-init).

The chart declares each Secret empty and the Application ignores its data, so
this Job needs only get and update on those names. A value is generated only
when it is absent, so reruns never rotate one:

  ITSAPLAN_SECRET  BETTER_AUTH_SECRET and APP_ENCRYPTION_KEY. Neither may ever
                   change: the first signs every session and the second
                   encrypts the stored sign-in and mail credentials.
  ADMIN_SECRET     The break-glass admin's email (ADMIN_EMAIL, written once)
                   and password.

ITSAPLAN_SECRET is generated freely only for an empty database. The Job runs
four steps, in order:

  db-state  (psql) counts the database's accounts, settings and stored
            secrets into DB_ROWS, read-only; 0 before the first migration.
  plan      (this script) decides. When the database holds rows and either
            key is missing, the Secret was lost: new keys would end every
            session and leave every stored credential undecryptable, so it
            refuses, unless the Secret carries ALLOW_ANNOTATION=true. Then it
            writes "purge" to PLAN.
  purge     (psql) on "purge", deletes every app_secret row. They are
            encrypted with the lost key, and itsaplan decrypts the stored
            OIDC settings before it accepts new ones, so without this every
            later write of them fails.
  apply     (this script) checks again and writes the keys. New main keys
            get a new generation ID, recorded as ROLL_ANNOTATION on the main
            Secret in the same write. Every run then sets that ID on the api's
            and worker's pod templates (ROLL), which restarts them only when it
            changed: running pods keep the old keys in their environment, and
            the PostSync bootstrap would otherwise store fresh settings under
            the old key through them. Because the ID is stored with the keys, a
            run that fails between the two writes is completed by the next.
            ArgoCD runs the bootstrap only once the pods are healthy again. A
            failed run repeats the purge, which is idempotent.

Either step removes ALLOW_ANNOTATION whenever it sees it, used or not, so a
stale annotation cannot authorise a later purge.

See the factory-cluster README, "itsaplan", for the recovery.

Uses only the standard library and the pod's service account token.
"""

import base64
import json
import os
import secrets
import ssl
import sys
import urllib.error
import urllib.request

SA = os.environ.get("SA_DIR", "/var/run/secrets/kubernetes.io/serviceaccount")
API = "https://{}:{}".format(
    os.environ["KUBERNETES_SERVICE_HOST"], os.environ["KUBERNETES_SERVICE_PORT"]
)
with open(f"{SA}/token") as f:
    TOKEN = f.read()
CTX = ssl.create_default_context(cafile=f"{SA}/ca.crt")
NAMESPACE = os.environ["NAMESPACE"]
MAIN = os.environ["ITSAPLAN_SECRET"]
ALLOW_ANNOTATION = "factory.jomcgi.dev/regenerate-itsaplan-keys"
# Set on the api's and worker's pod templates when new keys are written.
ROLL_ANNOTATION = "factory.jomcgi.dev/itsaplan-key-generation"

GENERATED = {
    MAIN: {
        # 32 random bytes, base64, as upstream's `openssl rand -base64 32`.
        "BETTER_AUTH_SECRET": lambda: base64.b64encode(secrets.token_bytes(32)),
        "APP_ENCRYPTION_KEY": lambda: base64.b64encode(secrets.token_bytes(32)),
    },
    os.environ["ADMIN_SECRET"]: {
        "email": lambda: os.environ["ADMIN_EMAIL"].encode(),
        "password": lambda: secrets.token_urlsafe(32).encode(),
    },
}


def call(method, path, body=None, content_type="application/json"):
    req = urllib.request.Request(
        API + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": content_type,
        },
    )
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404 and method == "PATCH":
            return None
        raise SystemExit(f"{method} {path}: {e.code} {e.read().decode()}")


def secret_path(name):
    return f"/api/v1/namespaces/{NAMESPACE}/secrets/{name}"


def database_rows():
    """Accounts, settings and stored secrets in itsaplan's database."""
    with open(os.environ["DB_ROWS"]) as f:
        text = f.read().strip()
    if not text.isdigit():
        raise SystemExit(
            f"db-state wrote {text!r} to {os.environ['DB_ROWS']}, not a row count; "
            "refusing to decide without one"
        )
    return int(text)


def regenerating(rows):
    """True when the main Secret's keys are missing and the loss is accepted.

    Refuses (exits) when they are missing from a database with rows and the
    Secret does not carry the annotation. An annotation that does not apply
    now is removed.
    """
    secret = call("GET", secret_path(MAIN))
    data = secret.get("data") or {}
    missing = [key for key in GENERATED[MAIN] if not data.get(key)]
    annotations = secret["metadata"].get("annotations") or {}
    allowed = annotations.get(ALLOW_ANNOTATION) == "true"
    if not missing or not rows:
        if ALLOW_ANNOTATION in annotations:
            annotations.pop(ALLOW_ANNOTATION)
            call("PUT", secret_path(MAIN), secret)
            print(f"removed the unused {ALLOW_ANNOTATION} annotation from {MAIN}")
        return False
    if allowed:
        return True
    raise SystemExit(
        f"REFUSING to generate {', '.join(missing)} in {NAMESPACE}/{MAIN}: the "
        f"database already holds {rows} account, settings and secret rows, so "
        "these keys existed and were lost. New ones would end every session and "
        "make every stored credential unreadable. Restore the old values into "
        "the Secret (a running api pod still holds them in its environment), or "
        "accept the loss with "
        f"`kubectl -n {NAMESPACE} annotate secret {MAIN} {ALLOW_ANNOTATION}=true` "
        'and sync again (factory-cluster README, "itsaplan").'
    )


def plan():
    rows = database_rows()
    print(f"itsaplan's database holds {rows} account, settings and secret rows")
    decision = "purge" if regenerating(rows) else "none"
    with open(os.environ["PLAN"], "w") as f:
        f.write(decision)
    print(f"plan: {decision}")


def roll(generation):
    """Set the key generation on the api's and worker's pod templates.

    A merge patch that sets the value they already carry changes nothing, so
    this runs every time and restarts them only after new keys.
    """
    patch = {
        "spec": {
            "template": {"metadata": {"annotations": {ROLL_ANNOTATION: generation}}}
        }
    }
    for name in os.environ["ROLL"].split():
        path = f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{name}"
        if call("PATCH", path, patch, "application/merge-patch+json") is None:
            print(f"{NAMESPACE}/{name} does not exist yet; it starts on the new keys")
        else:
            print(f"{NAMESPACE}/{name} is on key generation {generation}")


def apply():
    accepted = regenerating(database_rows())
    generation = None
    for name, keys in GENERATED.items():
        secret = call("GET", secret_path(name))
        data = dict(secret.get("data") or {})
        missing = [key for key in keys if not data.get(key)]
        for key in missing:
            data[key] = base64.b64encode(keys[key]()).decode()
        annotations = secret["metadata"].get("annotations") or {}
        if missing:
            secret["data"] = data
            if name == MAIN:
                if accepted:
                    annotations.pop(ALLOW_ANNOTATION)
                annotations[ROLL_ANNOTATION] = secrets.token_hex(8)
                secret["metadata"]["annotations"] = annotations
            call("PUT", secret_path(name), secret)
            print(f"added {', '.join(missing)} to {NAMESPACE}/{name}")
        else:
            print(f"{NAMESPACE}/{name} complete")
        if name == MAIN:
            generation = annotations.get(ROLL_ANNOTATION)
    # Keys written before generations were recorded carry none; their pods
    # already run on them.
    if generation:
        roll(generation)


if __name__ == "__main__":
    {"plan": plan, "apply": apply}[sys.argv[1]]()
