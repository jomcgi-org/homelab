# echo function: the zip-lane end-to-end smoke test

`app.py` is a minimal `handler(event, context)` (`app.handle`) that echoes the
event the shim marshaled from the inbound request back as a JSON response body.
It is the smoke-test fixture that proves the whole EmberVM zip lane (ADR
embervm/002) end to end, and the fixture later FaaS PRs reuse.

## The fixture

- `app.py` -- the echo handler (`def handle(event, context)`), stdlib-only.
- `echo.zip` -- a checked-in, reproducibly-built zip of `app.py` at the archive
  root (so the shim imports `app.handle`).

`echo.zip` is deterministic (fixed member metadata: name `app.py`, timestamp
`2000-01-01`, mode `0644`, deflate), so its sha256 is stable across rebuilds.

**sha256 of `echo.zip`:**

```
53ff98ccb09d4d12a629322caac8ee0aee9f77ca69fd08fbc1eee83b7a60230b
```

To rebuild it after editing `app.py` (this recomputes the sha256; if it changes,
update `workload-echo-fn.yaml` and this file):

```bash
cd projects/embervm/runtimes/python/testdata/echo
python3 - <<'EOF'
import zipfile, hashlib
info = zipfile.ZipInfo(filename="app.py", date_time=(2000, 1, 1, 0, 0, 0))
info.compress_type = zipfile.ZIP_DEFLATED
info.external_attr = 0o644 << 16
with zipfile.ZipFile("echo.zip", "w") as zf:
    zf.writestr(info, open("app.py", "rb").read())
print(hashlib.sha256(open("echo.zip", "rb").read()).hexdigest())
EOF
```

## The event / response contract (what the echo returns)

The shim (see `../../shim.py`, README section "Event and response shape") calls
`handle(event, context)` where `event` is:

```json
{
  "httpMethod": "POST",
  "path": "/invoke",
  "queryStringParameters": {"k": "v"} | null,
  "headers": {"Content-Type": "..."},
  "body": "<utf-8 body>" | null,
  "isBase64Encoded": false
}
```

`handle` returns an explicit response dict:

```json
{
  "statusCode": 200,
  "headers": {"Content-Type": "application/json"},
  "body": "<json.dumps(event)>",
  "isBase64Encoded": false
}
```

So a `POST /invoke` with body `hello ember` gets back a `200` whose JSON body is
the event dict, whose `.body` field is `"hello ember"`. That is the round-trip.

## LIVE end-to-end procedure (run against the homelab cluster)

Prereqs: `kubectl` pointed at the homelab cluster, `rclone`, `curl`. SeaweedFS
S3 is in-cluster only (ClusterIP `seaweedfs-s3.seaweedfs:8333`), so upload over a
`kubectl port-forward` (mirror `projects/monolith/grimoire/tools/upload-book.sh`).

### 1. Upload the zip to SeaweedFS at `s3://faas/echo-fn/<sha256>.zip`

```bash
SHA=53ff98ccb09d4d12a629322caac8ee0aee9f77ca69fd08fbc1eee83b7a60230b
ZIP=projects/embervm/runtimes/python/testdata/echo/echo.zip

# Sanity: the local bytes must match the sha the CR pins.
test "$(sha256sum "$ZIP" | cut -d' ' -f1)" = "$SHA" || echo "SHA MISMATCH"

# rclone SeaweedFS remote, configured purely via env (no config file). The
# duckdb/duckdb identity matches the SEAWEEDFS_S3 creds in the cluster.
export RCLONE_CONFIG_SW_TYPE=s3
export RCLONE_CONFIG_SW_PROVIDER=Other
export RCLONE_CONFIG_SW_ACCESS_KEY_ID=duckdb
export RCLONE_CONFIG_SW_SECRET_ACCESS_KEY=duckdb
export RCLONE_CONFIG_SW_ENDPOINT=http://localhost:8333
export RCLONE_CONFIG_SW_REGION=us-east-1
export RCLONE_CONFIG_SW_FORCE_PATH_STYLE=true

kubectl port-forward -n seaweedfs svc/seaweedfs-s3 8333:8333 >/dev/null 2>&1 &
PF=$!
sleep 2
rclone mkdir SW:faas 2>/dev/null || true
rclone copyto "$ZIP" "SW:faas/echo-fn/${SHA}.zip"
rclone ls "SW:faas/echo-fn/"   # expect: 1078 echo-fn/<sha>.zip
kill "$PF"
```

The CR's `codeUri` is the in-cluster read URL for exactly this object:
`http://seaweedfs-s3.seaweedfs.svc.cluster.local:8333/faas/echo-fn/<sha256>.zip`.

### 2. Apply the Workload CR

```bash
kubectl apply -f projects/embervm/crd/samples/workload-echo-fn.yaml

# Wait for the base to build (control plane -> noded BuildBase -> snapshot):
kubectl -n embervm wait workload/echo-fn --for=condition=Ready --timeout=180s
kubectl -n embervm get workload echo-fn -o jsonpath='{.status}{"\n"}'
# expect conditions Ready=True/BaseReady and BaseBuilt=True/BaseBuilt, a
# snapshotRef, and (once the pool warms the floor) primedFloorSatisfied.
```

### 3. Submit a task and read the echoed response

The submit API is on the control-plane service (`embervm-control`, port 8080)
and needs a bearer token for an allow-listed service account
(`system:serviceaccount:embervm:embervm`). From inside the cluster:

```bash
# Mint a token for the embervm SA (or reuse an existing one).
TOKEN=$(kubectl -n embervm create token embervm)

# Port-forward the control-plane submit API.
kubectl -n embervm port-forward svc/embervm-control 8080:8080 >/dev/null 2>&1 &
PF=$!
sleep 2

# Synchronous submit: POST the payload, wait for the restored VM's response.
curl -sS -X POST \
  "http://127.0.0.1:8080/v1/workloads/echo-fn/tasks?wait=true" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: text/plain" \
  --data 'hello ember'
echo
kill "$PF"
```

**Expected response** (HTTP 200, `application/json`): the marshaled event, whose
`body` field is the payload you sent:

```json
{
  "httpMethod": "POST",
  "path": "/invoke",
  "queryStringParameters": null,
  "headers": {"Content-Type": "text/plain", "Content-Length": "11", "...": "..."},
  "body": "hello ember",
  "isBase64Encoded": false
}
```

The `headers` map reflects whatever the request carried, so exact keys vary; the
assertions that matter are `body == "hello ember"` and `httpMethod == "POST"`.

**Timing:** the first `wait=true` after a fresh apply may 202-timeout (async)
while the base is still building or the floor is priming; once the floor is
warm, a restore-and-invoke round-trip returns in well under the 30s invocation
timeout (a warm restore is sub-second; a cold miss adds the prime). Re-submit if
you get a 202 with `state: queued`.

### 4. Cleanup

```bash
kubectl delete -f projects/embervm/crd/samples/workload-echo-fn.yaml
# Optionally remove the uploaded object (re-port-forward + rclone if needed):
#   rclone deletefile "SW:faas/echo-fn/${SHA}.zip"
```

Deleting the CR forgets the workload in the control plane (WorkloadWatcher DELETE
-> catalog drop -> BaseBuilder forget), so the base and any primed VMs are torn
down.
