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
script only updates those names. Library consumers also declare labeled
Secrets, discovered through metadata lists in namespaces that grant list access.

Uses only the standard library and the pod's service account token.
"""

import base64
import json
import os
import re
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
# (required anti-affinity), so the pod's hostname is its zone. The first
# layout waits for all of them; later runs need only a write quorum.
NODES = 3
QUORUM = 2
CAPACITY = 50 * 10**9  # bytes per node; used for balancing, not enforced

BUCKETS = [
    "factory-itsaplan",
    "factory-evidence",
    "ate-snapshots",
    "kagent-pg-backups",
    "factory-db-backups",
    "authentik-pg-backups",
]
# (bucket, namespace, Secret). ate-snapshots is granted to ate-system when
# Substrate moves its snapshots from rustfs.
GRANTS = [
    ("factory-itsaplan", "factory", "factory-itsaplan-s3"),
    ("factory-evidence", "factory", "factory-evidence-s3"),
    ("kagent-pg-backups", "kagent-pg", "kagent-pg-s3"),
    ("factory-db-backups", "factory-db", "factory-db-s3"),
    ("authentik-pg-backups", "authentik", "authentik-pg-s3"),
]


def k8s(method, path, body=None, metadata_only=False):
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
            "Accept": (
                "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1"
                if metadata_only
                else "application/json"
            ),
        },
    )
    ctx = ssl.create_default_context(cafile=f"{SA}/ca.crt")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        # 403: the consumer's Role is not there yet, the same as a missing Secret.
        if e.code in (403, 404) and method == "GET":
            return None
        raise SystemExit(f"{method} {path}: {e.code}")


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
        raise SystemExit(f"{method} {endpoint}: {e.code}")


def wait_for_nodes(timeout=600):
    deadline = time.monotonic() + timeout
    while True:
        try:
            # With a layout in place a node down for maintenance must not
            # fail the run: two of three still serve every read and write.
            need = QUORUM if garage("GET", "GetClusterLayout")["roles"] else NODES
            # A peer's hostname arrives with its first status exchange, a
            # few seconds after it connects; it names the node's zone.
            status = garage("GET", "GetClusterStatus")["nodes"]
            nodes = [n for n in status if n["isUp"] and n.get("hostname")]
            if len(nodes) >= need:
                if len(nodes) < NODES:
                    print(f"WARNING: only {len(nodes)} of {NODES} Garage nodes up")
                return nodes
            print(f"{len(nodes)} of {NODES} Garage nodes up, {need} needed; waiting")
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


def ensure_grant(bucket_id, bucket, namespace, name, access="readwrite"):
    secret = k8s("GET", secret_path(namespace, name))
    if secret is None:
        print(f"{namespace}/{name} not present yet; skipping {bucket}")
        return
    data = dict(secret.get("data") or {})
    if ("AWS_ACCESS_KEY_ID" in data) != ("AWS_SECRET_ACCESS_KEY" in data):
        raise RuntimeError(
            f"{namespace}/{name}: incomplete key pair; refusing rotation"
        )
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
    info = garage(
        "GET",
        "GetKeyInfo",
        query={"id": key_id, "showSecretKey": "true"},
        missing_ok=True,
    )
    if info is not None and info.get("secretAccessKey") != key_secret:
        # Never overwrite either side: the Secret may be stale, or Garage's
        # key may have been changed by hand. An operator decides (README).
        raise RuntimeError(f"{namespace}/{name}: Garage key has a different secret")
    if info is None:
        garage(
            "POST",
            "ImportKey",
            {
                "accessKeyId": key_id,
                "secretAccessKey": key_secret,
                "name": f"{namespace}/{name}",
            },
        )
        print(f"imported key {namespace}/{name}")
    # A claim renamed to another bucket must not retain its old grant.
    for previous in (info or {}).get("buckets", []):
        if previous["id"] != bucket_id:
            garage(
                "POST",
                "DenyBucketKey",
                {
                    "bucketId": previous["id"],
                    "accessKeyId": key_id,
                    "permissions": {"read": True, "write": True, "owner": True},
                },
            )
    permissions = {
        "read": access in ("read", "readwrite"),
        "write": access in ("write", "readwrite"),
        "owner": False,
    }
    # AllowBucketKey is additive. Revoke unwanted rights on every reconcile,
    # including owner, so a values change can narrow access without key rotation.
    garage(
        "POST",
        "DenyBucketKey",
        {
            "bucketId": bucket_id,
            "accessKeyId": key_id,
            "permissions": {key: not value for key, value in permissions.items()},
        },
    )
    garage(
        "POST",
        "AllowBucketKey",
        {
            "bucketId": bucket_id,
            "accessKeyId": key_id,
            "permissions": permissions,
        },
    )
    print(f"{namespace}/{name} has {access} on {bucket}")


CLAIM_LABEL = "factory.jomcgi.dev/garage-bucket"
ACCESS_LABEL = "factory.jomcgi.dev/garage-access"


def list_metadata(path, **query):
    """Paginate metadata only; a namespace without a discovery Role is skipped."""
    query["limit"] = 100
    while True:
        page = k8s(
            "GET", path + "?" + urllib.parse.urlencode(query), metadata_only=True
        )
        if page is None:
            if path == "/api/v1/namespaces":
                raise RuntimeError("namespace discovery is not authorized")
            return
        yield from page.get("items", [])
        token = page.get("metadata", {}).get("continue")
        if not token:
            return
        query["continue"] = token


def discover_grants():
    # Namespace enumeration carries no Secret access. Consumers opt in with a
    # namespaced list Role; updates remain restricted to exact claim names.
    for namespace in list_metadata("/api/v1/namespaces"):
        ns = namespace["metadata"]["name"]
        for secret in list_metadata(
            f"/api/v1/namespaces/{ns}/secrets", labelSelector=CLAIM_LABEL
        ):
            meta = secret["metadata"]
            labels = meta.get("labels", {})
            bucket = labels.get(CLAIM_LABEL, "")
            access = labels.get(ACCESS_LABEL, "")
            if not re.fullmatch(
                r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", bucket
            ) or access not in ("read", "write", "readwrite"):
                raise ValueError(f"invalid Garage claim {ns}/{meta['name']}")
            yield bucket, ns, meta["name"], access


def bootstrap():
    nodes = wait_for_nodes()
    ensure_layout(nodes)
    ids = {name: ensure_bucket(name) for name in BUCKETS}
    failed = []
    grants = [(*grant, "readwrite") for grant in GRANTS]
    try:
        grants.extend(discover_grants())
    except (Exception, SystemExit) as e:
        print(f"ERROR: claim discovery: {e}")
        failed.append("claim discovery")
    # One bad grant must not stop the others.
    for bucket, namespace, name, access in grants:
        try:
            if bucket not in ids:
                ids[bucket] = ensure_bucket(bucket)
            ensure_grant(ids[bucket], bucket, namespace, name, access)
        except (Exception, SystemExit) as e:
            print(f"ERROR: {namespace}/{name}: {e}")
            failed.append(f"{namespace}/{name}")
    health = garage("GET", "GetClusterHealth")
    print(
        f"cluster {health['status']}: {health['storageNodesUp']}/{health['storageNodes']} "
        f"storage nodes up, {health['partitionsAllOk']}/{health['partitions']} partitions all ok"
    )
    if failed:
        raise SystemExit(f"grants failed: {', '.join(failed)}")


if __name__ == "__main__":
    {"secrets": ensure_secrets, "bootstrap": bootstrap}[sys.argv[1]]()
