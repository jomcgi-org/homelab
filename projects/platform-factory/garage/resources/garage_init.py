"""Create Garage's secrets, layout, buckets and per-bucket keys, idempotently.

Two modes, both safe to rerun:

  secrets    Fill garage/garage-secrets (RPC secret, admin token) once. Runs
             before the StatefulSet; an existing value is never rotated.
  bootstrap  Wait for the three nodes, assign the initial layout, create the
             buckets, and give each consuming namespace its own key. Runs
             after each sync and on a schedule, so a consumer whose Secret
             appears later is served on the next run.

The consumer's Secret is the source of truth for its key: credentials are
generated into it when absent and imported into Garage when Garage lacks
them, so a rerun never rotates a key and a rebuilt Garage gets the same keys
back. ArgoCD creates every Secret named here empty and ignores its data; this
script only reads and updates those names.

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
import urllib.parse
import urllib.request

SA = "/var/run/secrets/kubernetes.io/serviceaccount"
NAMESPACE = "garage"
SECRETS = "garage-secrets"
ADMIN = os.environ.get("GARAGE_ADMIN_URL", "http://garage-admin.garage.svc:3903")
S3_ENDPOINT = "http://garage.garage.svc:3900"
REGION = "garage"

# Garage pods and the initial layout. Each pod runs on its own node
# (required anti-affinity), so the pod's hostname is its zone.
NODES = 3
CAPACITY = 50 * 10**9  # bytes per node; used for balancing, not enforced

BUCKETS = ["factory-itsaplan", "factory-evidence", "ate-snapshots", "kagent-pg-backups"]
# (bucket, namespace, Secret). ate-snapshots is granted to ate-system when
# Substrate moves its snapshots from rustfs.
GRANTS = [
    ("factory-itsaplan", "factory", "factory-itsaplan-s3"),
    ("factory-evidence", "factory", "factory-evidence-s3"),
    ("kagent-pg-backups", "kagent", "kagent-pg-s3"),
]


def k8s(method, path, body=None):
    api = "https://{}:{}".format(
        os.environ["KUBERNETES_SERVICE_HOST"], os.environ["KUBERNETES_SERVICE_PORT"]
    )
    with open(f"{SA}/token") as f:
        token = f.read()
    req = urllib.request.Request(
        api + path,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        # 403: the consumer's Role is not there yet, the same as a missing Secret.
        if e.code in (403, 404) and method == "GET":
            return None
        raise SystemExit(f"{method} {path}: {e.code} {e.read().decode()}")


def secret_path(namespace, name):
    return f"/api/v1/namespaces/{namespace}/secrets/{name}"


def b64(value):
    return base64.b64encode(value.encode()).decode()


def unb64(value):
    return base64.b64decode(value).decode()


def ensure_secrets():
    source = k8s("GET", secret_path(NAMESPACE, SECRETS))
    if source is None:
        raise SystemExit(f"{NAMESPACE}/{SECRETS} is missing; ArgoCD creates it")
    data = dict(source.get("data") or {})
    generated = {
        # 32 bytes, hex encoded, as Garage requires.
        "rpc-secret": lambda: secrets.token_hex(32),
        "admin-token": lambda: secrets.token_urlsafe(32),
    }
    missing = [key for key in generated if key not in data]
    for key in missing:
        data[key] = b64(generated[key]())
    if missing:
        source["data"] = data
        k8s("PUT", secret_path(NAMESPACE, SECRETS), source)
        print(f"added {', '.join(missing)} to {NAMESPACE}/{SECRETS}")
    else:
        print(f"{NAMESPACE}/{SECRETS} complete")


def garage(method, endpoint, body=None, query=None, missing_ok=False):
    url = f"{ADMIN}/v2/{endpoint}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(
        url,
        method=method,
        data=None if body is None else json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {os.environ['GARAGE_ADMIN_TOKEN']}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code == 404 and missing_ok:
            return None
        raise SystemExit(f"{method} {endpoint}: {e.code} {e.read().decode()}")


def wait_for_nodes(timeout=600):
    deadline = time.monotonic() + timeout
    while True:
        try:
            nodes = [n for n in garage("GET", "GetClusterStatus")["nodes"] if n["isUp"]]
            if len(nodes) >= NODES:
                return nodes
            print(f"{len(nodes)} of {NODES} Garage nodes up; waiting")
        except (urllib.error.URLError, OSError) as e:
            print(f"Garage admin API not answering ({e}); waiting")
        if time.monotonic() > deadline:
            raise SystemExit("Garage nodes did not come up")
        time.sleep(10)


def ensure_layout(nodes):
    layout = garage("GET", "GetClusterLayout")
    if layout["roles"]:
        assigned = {role["id"] for role in layout["roles"]}
        new = [n["id"] for n in nodes if n["id"] not in assigned]
        print(f"layout version {layout['version']} has {len(assigned)} nodes")
        if new:
            # A node with a new ID means a pod lost its metadata volume. Its
            # replacement in the layout is an operator decision (README).
            print(f"WARNING: nodes without a role: {', '.join(new)}")
        return
    roles = [
        {
            "id": n["id"],
            "zone": n["hostname"],
            "capacity": CAPACITY,
            "tags": [n["hostname"]],
        }
        for n in nodes
    ]
    garage(
        "POST",
        "UpdateClusterLayout",
        {"roles": roles, "parameters": {"zoneRedundancy": "maximum"}},
    )
    garage("POST", "ApplyClusterLayout", {"version": layout["version"] + 1})
    print(f"applied layout version {layout['version'] + 1} with {len(roles)} nodes")


def ensure_bucket(name):
    info = garage("GET", "GetBucketInfo", query={"globalAlias": name}, missing_ok=True)
    if info is None:
        info = garage("POST", "CreateBucket", {"globalAlias": name})
        print(f"created bucket {name}")
    return info["id"]


def ensure_grant(bucket_id, bucket, namespace, name):
    secret = k8s("GET", secret_path(namespace, name))
    if secret is None:
        print(f"{namespace}/{name} not present yet; skipping {bucket}")
        return
    data = dict(secret.get("data") or {})
    if "AWS_ACCESS_KEY_ID" in data and "AWS_SECRET_ACCESS_KEY" in data:
        key_id = unb64(data["AWS_ACCESS_KEY_ID"])
        key_secret = unb64(data["AWS_SECRET_ACCESS_KEY"])
    else:
        key_id = "GK" + secrets.token_hex(12)
        key_secret = secrets.token_hex(32)
    want = {
        "AWS_ACCESS_KEY_ID": b64(key_id),
        "AWS_SECRET_ACCESS_KEY": b64(key_secret),
        "AWS_ENDPOINT_URL": b64(S3_ENDPOINT),
        "AWS_REGION": b64(REGION),
        "BUCKET": b64(bucket),
    }
    if any(data.get(k) != v for k, v in want.items()):
        data.update(want)
        secret["data"] = data
        # Written before the key exists in Garage, so a failure in between
        # leaves credentials that the next run imports, never a lost key.
        k8s("PUT", secret_path(namespace, name), secret)
        print(f"updated {namespace}/{name}")
    if garage("GET", "GetKeyInfo", query={"id": key_id}, missing_ok=True) is None:
        garage(
            "POST",
            "ImportKey",
            {"accessKeyId": key_id, "secretAccessKey": key_secret, "name": f"{namespace}/{name}"},
        )
        print(f"imported key {namespace}/{name}")
    garage(
        "POST",
        "AllowBucketKey",
        {
            "bucketId": bucket_id,
            "accessKeyId": key_id,
            "permissions": {"read": True, "write": True, "owner": False},
        },
    )
    print(f"{namespace}/{name} has read and write on {bucket}")


def bootstrap():
    nodes = wait_for_nodes()
    ensure_layout(nodes)
    ids = {name: ensure_bucket(name) for name in BUCKETS}
    for bucket, namespace, name in GRANTS:
        ensure_grant(ids[bucket], bucket, namespace, name)
    health = garage("GET", "GetClusterHealth")
    print(
        f"cluster {health['status']}: {health['storageNodesUp']}/{health['storageNodes']} "
        f"storage nodes up, {health['partitionsAllOk']}/{health['partitions']} partitions all ok"
    )


if __name__ == "__main__":
    {"secrets": ensure_secrets, "bootstrap": bootstrap}[sys.argv[1]]()
