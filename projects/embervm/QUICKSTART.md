# Standalone EmberVM quickstart

This path boots the published EmberVM control plane and one noded on one Linux
host, then runs a hello-world task and an Ember-native session lifecycle. It
uses the chart's shipped Workload CRD and HTTP API. It does not use the
homelab's Argo CD applications, k3s fleet layout, SeaweedFS, 1Password
Operator, Cloudflare Tunnel, or observability stack.

The profile is intentionally local and single-node. It is suitable for an
evaluation host, not a highly available installation.

## What gets installed

- One existing k3s node, labelled for EmberVM.
- The published `embervm` OCI chart, pinned by `install.sh` to version
  `0.78.2`. The package already contains immutable image digests produced by
  repository CI.
- The control plane with a 1 GiB SQLite op-log PVC.
- One privileged noded DaemonSet with `/dev/kvm`, a 4 GiB pod memory ceiling,
  and only the published Python zip runtime rootfs.
- A 12 GiB host loop-backed ext4 scratch mount at
  `/var/lib/embervm/scratch`.
- A local MinIO service with a 4 GiB PVC for base, bank, and function
  artifacts.
- `hello` task and `continuity` session Workloads. Their zip archives are
  uploaded to MinIO before the Workloads are registered.

The generated object-store and noded bearer values live only in Kubernetes
Secrets. No secret is stored in this directory or passed as a Helm value.

## Prerequisites

Use a disposable or dedicated x86_64 Linux host. The published control-plane,
noded, and Python runtime images are amd64-only.

- Linux with systemd, hardware virtualization enabled in firmware, and Intel
  VT-x (`vmx`) or AMD-V (`svm`) visible to the kernel.
- `/dev/kvm` available to containers. A VM host must expose nested
  virtualization. Merely seeing a CPU flag inside a VM is insufficient if the
  hypervisor withholds `/dev/kvm`.
- One-node k3s with its default `local-path` StorageClass. Install k3s before
  this walkthrough and make sure `kubectl get nodes` reports exactly one Ready
  node. The installer deliberately refuses a multi-node context.
- Helm 3.18 or newer, `kubectl`, Python 3.11 or newer, `curl`, `jq`,
  `sha256sum`, and `od`.
- Outbound HTTPS access to `ghcr.io` and `quay.io`. The chart and EmberVM
  images are pulled from GHCR. MinIO and its client are pulled from Quay.
- At least 4 CPU threads, 8 GiB RAM, and 30 GiB free under `/var/lib`.
  Twelve GiB RAM is more comfortable during the initial rootfs build. Each
  quickstart guest declares 512 MiB plus 64 MiB host overhead, while k3s,
  MinIO, the control plane, noded, image extraction, and page cache need the
  remaining host memory.

The scratch preparer is privileged and enters the host mount namespace. On a
host where `/var/lib/embervm/scratch` is not already a mount, it creates
`/var/lib/embervm/scratch.img`, formats it as ext4, mounts it, and adds the exact
mount to `/etc/fstab`. If the path is already a mountpoint, it leaves that
mount intact and uses it. Review `standalone/platform.yaml` and the chart's
`scratch-prep-daemonset.yaml` before using a shared host.

Run the read-only checks first:

```bash
projects/embervm/standalone/host-check.sh
kubectl get nodes -o wide
kubectl get storageclass local-path
```

Do not continue until every check passes. Membership in a desktop container
runtime's `kvm` group does not prove that a k3s container can open `/dev/kvm`.

## Acquire and install the artifacts

Clone this repository so the deterministic function fixture and the standalone
profile are present. The runtime images are not rebuilt on the host.

```bash
git clone https://github.com/jomcgi-org/homelab.git
cd homelab
helm pull oci://ghcr.io/jomcgi/homelab/charts/embervm \
  --version 0.78.2 --destination /tmp
helm show chart /tmp/embervm-0.78.2.tgz
```

`helm pull` is an optional acquisition check. `install.sh` installs the same OCI
reference directly. It builds the 1.6 KiB continuity zip deterministically
from `standalone/functions/continuity/app.py`; the hello zip is the existing
runtime fixture. It verifies both SHA-256 values before uploading them.

Make sure the current `kubectl` context is the dedicated one-node k3s cluster,
then install:

```bash
projects/embervm/standalone/install.sh
```

The script is idempotent for an existing quickstart namespace. It preserves
existing generated Secrets, recreates the artifact upload Job, upgrades the
same Helm release, waits for scratch, control-plane, and noded readiness,
registers both Workloads, and waits for both base snapshots to become Ready.

To test a later published chart without editing the script:

```bash
EMBER_QUICKSTART_CHART_VERSION=0.78.2 \
  projects/embervm/standalone/install.sh
```

Changing the version changes every chart-pinned component together. Do not
override individual image tags or digests.

## Ordinary workload walkthrough

The commands below are the consumer path. Host, storage, and component checks
are kept in the operator section later.

Start a control-plane port-forward and mint a short-lived management token for
the chart's allow-listed ServiceAccount:

```bash
kubectl -n embervm port-forward service/embervm-embervm 8080:8080 \
  >/tmp/embervm-port-forward.log 2>&1 &
EMBER_PORT_FORWARD_PID=$!
EMBER_TOKEN=$(kubectl -n embervm create token embervm-embervm --duration=1h)
curl --fail --silent --show-error http://127.0.0.1:8080/healthz
```

### Register the workloads

`install.sh` already applied these definitions. Applying them again shows the
native registration interface and is safe:

```bash
kubectl -n embervm apply \
  -f projects/embervm/standalone/workloads/hello.yaml \
  -f projects/embervm/standalone/workloads/continuity.yaml
kubectl -n embervm wait workload/hello workload/continuity \
  --for=condition=Ready --timeout=10m
kubectl -n embervm get workload hello continuity
```

A Workload is a definition. Session instances and task executions are
control-plane records, not Kubernetes objects.

### Run hello world

Submit one synchronous task through the shipped HTTP interface:

```bash
HELLO_RESULT=$(curl --fail --silent --show-error \
  -X POST 'http://127.0.0.1:8080/v1/workloads/hello/tasks?wait=true' \
  -H "Authorization: Bearer ${EMBER_TOKEN}" \
  -H 'Content-Type: text/plain' \
  --data 'hello EmberVM')
printf '%s\n' "${HELLO_RESULT}" | jq .
test "$(printf '%s' "${HELLO_RESULT}" | jq -r .body)" = 'hello EmberVM'
```

The result is the event returned by the shipped Python zip runtime. The final
`test` is the hello-world acceptance check, not just an HTTP health check.

### Create a session and execute an operation

Create returns a session capability once. Keep it in shell variables and do
not write it to a file or paste it into logs:

```bash
SESSION_CREATE=$(curl --fail --silent --show-error \
  -X POST http://127.0.0.1:8080/v1/workloads/continuity/sessions \
  -H "Authorization: Bearer ${EMBER_TOKEN}" \
  -H 'Idempotency-Key: standalone-continuity-1')
SESSION_ID=$(printf '%s' "${SESSION_CREATE}" | jq -er .session_id)
SESSION_TOKEN=$(printf '%s' "${SESSION_CREATE}" | jq -er .session_token)
printf '%s\n' "${SESSION_CREATE}" | jq 'del(.session_token)'

curl --fail --silent --show-error \
  -X POST "http://127.0.0.1:8080/v1/sessions/${SESSION_ID}/invoke" \
  -H "Authorization: Bearer ${SESSION_TOKEN}" \
  -H 'Content-Type: application/json' \
  --data '{"op":"write","value":"survived bank and relight"}' | jq .
```

The operation writes `/tmp/ember-quickstart-workspace/marker.txt` inside the
session VM. That writable tmpfs is part of this memory-persistent session.

### Inspect status and logs

Read the session with its own capability:

```bash
curl --fail --silent --show-error \
  http://127.0.0.1:8080/v1/sessions/${SESSION_ID} \
  -H "Authorization: Bearer ${SESSION_TOKEN}" | jq .
```

Consumer-facing guest output is the invoke response. EmberVM does not ship a
native streaming guest-log API. On this operator-owned quickstart host,
component lifecycle logs are available from Kubernetes:

```bash
kubectl -n embervm logs deployment/embervm-embervm \
  -c control-plane --since=10m
kubectl -n embervm logs daemonset/embervm-embervm-noded \
  -c noded --since=10m
```

### Suspend, resume, and prove continuity

There is no explicit native suspend endpoint. For a session, suspend is the
normal idle-bank transition. The `continuity` Workload requests banking after
5 idle seconds; the control-plane sweep runs every 30 seconds, so allow up to
45 seconds. Wait until status reports `banked`:

```bash
for attempt in $(seq 1 18); do
  SESSION_STATE=$(curl --fail --silent --show-error \
    http://127.0.0.1:8080/v1/sessions/${SESSION_ID} \
    -H "Authorization: Bearer ${SESSION_TOKEN}" | jq -r .state)
  printf 'state=%s\n' "${SESSION_STATE}"
  test "${SESSION_STATE}" = banked && break
  sleep 5
done
test "${SESSION_STATE}" = banked
```

The next invoke is the native resume trigger. Read the marker rather than
rewriting it:

```bash
RESUME_RESULT=$(curl --fail --silent --show-error \
  -X POST "http://127.0.0.1:8080/v1/sessions/${SESSION_ID}/invoke" \
  -H "Authorization: Bearer ${SESSION_TOKEN}" \
  -H 'Content-Type: application/json' \
  --data '{"op":"read"}')
printf '%s\n' "${RESUME_RESULT}" | jq .
test "$(printf '%s' "${RESUME_RESULT}" | jq -r .value)" = \
  'survived bank and relight'
```

That final assertion demonstrates workspace continuity across an observed
`banked` state and a relight. A successful status transition without the value
assertion is not continuity evidence.

### Delete the session and definitions

Session deletion is a management operation, so it uses the management token:

```bash
curl --fail --silent --show-error \
  -X DELETE "http://127.0.0.1:8080/v1/sessions/${SESSION_ID}" \
  -H "Authorization: Bearer ${EMBER_TOKEN}" | jq .
kubectl -n embervm delete workload hello continuity
kill "${EMBER_PORT_FORWARD_PID}"
unset SESSION_TOKEN EMBER_TOKEN
```

Deleting a Workload removes its catalog entry and allows EmberVM to forget its
reconstructible base. Delete session instances first so their lifecycle can
finish against the still-registered definition.

## Operator checks

These checks diagnose the substrate. Ordinary workload callers should not need
brick, scratch, or object-store details.

### KVM and host capacity

```bash
projects/embervm/standalone/host-check.sh
kubectl get node -l homelab.io/firecracker=true
kubectl -n embervm get pod -l app.kubernetes.io/component=noded -o wide
```

If `/dev/kvm` exists on the host but noded cannot open it, check the k3s service
confinement and host device permissions. Do not replace KVM with software
emulation; noded requires the Firecracker KVM API.

### Scratch persistence

The scratch DaemonSet is Ready only after the path is a host mountpoint and its
generation marker exists:

```bash
kubectl -n embervm get daemonset embervm-embervm-scratch-prep
kubectl -n embervm logs daemonset/embervm-embervm-scratch-prep --since=10m
kubectl -n embervm exec daemonset/embervm-embervm-scratch-prep -- \
  nsenter -t 1 -m -- sh -c \
  'mountpoint -q /var/lib/embervm/scratch && test -s /var/lib/embervm/scratch/.scratch-generation'
```

The ext4 mount and its `/etc/fstab` entry survive pod and host restarts. The
scratch holds rootfs files and local warmth. It is not shared storage and does
not survive loss of the host disk.

### Object-store connectivity

MinIO persists to a `local-path` PVC. The upload Job proves authenticated PUTs
to both required buckets:

```bash
kubectl -n embervm get pvc embervm-minio
kubectl -n embervm get deployment embervm-minio
kubectl -n embervm logs job/embervm-fixture-upload
kubectl -n embervm port-forward service/embervm-minio 9000:9000 \
  >/tmp/embervm-minio-port-forward.log 2>&1 &
EMBER_MINIO_FORWARD_PID=$!
curl --fail http://127.0.0.1:9000/minio/health/ready
kill "${EMBER_MINIO_FORWARD_PID}"
```

Banked artifacts can be exported to MinIO, but this single-host MinIO PVC is on
the same machine. It demonstrates the object-store interface and persistence
across pod restarts, not host-failure durability.

### Component readiness

```bash
kubectl -n embervm get deployment,daemonset,pod
kubectl -n embervm get workload hello continuity -o yaml
kubectl -n embervm port-forward service/embervm-embervm 8080:8080
```

In another shell:

```bash
EMBER_TOKEN=$(kubectl -n embervm create token embervm-embervm --duration=10m)
curl --fail http://127.0.0.1:8080/healthz
curl --fail http://127.0.0.1:8080/livez
curl --fail http://127.0.0.1:8080/v1/nodes \
  -H "Authorization: Bearer ${EMBER_TOKEN}" | jq .
```

The control-plane `/healthz` proves its supervised managers are present.
Noded is Ready only after the control plane has pushed its live workload
registry. `/v1/nodes` is the combined view of node health, inventory, memory
headroom, and recent placement denials.

## Troubleshooting

### Unavailable capacity

Session create returns HTTP 429 with `reason: no_capacity`, `workload_cap`, or
`session_cap` when the node cannot admit the request. A hard brick limit can
return HTTP 503 with `reason: fleet_full`. Inspect `/v1/nodes`, noded pod memory,
and existing sessions. Delete abandoned sessions or wait for an in-flight task
to finish. Raising `noded.resources.limits.memory` or workload concurrency
without adding real host RAM defeats admission and can invoke the host OOM
killer.

### Image preparation failure

If a Workload never reaches Ready, inspect its conditions and the one rootfs
builder:

```bash
kubectl -n embervm get workload hello -o yaml
kubectl -n embervm get pods -l app.kubernetes.io/component=noded
kubectl -n embervm logs daemonset/embervm-embervm-noded \
  -c build-runtime-python-rootfs
kubectl -n embervm logs job/embervm-fixture-upload
```

An image pull error points to GHCR reachability. A rootfs build error points to
scratch space or the published runtime image. An archive GET or checksum error
points to MinIO, the upload Job, or a mismatch between `codeUri`, `sha256`, and
the uploaded bytes. Re-run `install.sh`; it verifies and re-uploads both
archives before reapplying the Workloads.

### Guest readiness failure

The Python runtime answers `/shim/ready` only after its archive is downloaded,
SHA-256 verified, unpacked, and `app.handle` imported. Inspect the Workload
conditions and noded logs for `BuildBase`, archive, import, or ready-probe
errors. Do not treat a healthy control plane or a successful Helm render as a
guest boot. The acceptance signal is a Ready Workload followed by the real
hello response or continuity assertion.

### Unsupported capability

The native surface has Workload registration, task submit/result APIs, session
create/invoke/status/delete, and automatic idle bank/relight. It does not
provide an explicit session suspend or resume verb, generic exec/attach,
port-forward into a guest, or a streaming guest-log API. Use the documented
automatic bank plus invoke-triggered relight. Do not translate an unsupported
operation into an unrelated task route.

EmberVM is not currently compatible with the Kubernetes SIG Agent Sandbox API.
ADR 004 accepted a future edge adapter, but that adapter is not shipped and its
compatibility gate remains under review in
[#5806](https://github.com/jomcgi-org/homelab/issues/5806). This quickstart is
Ember-native and does not install or emulate `Sandbox`, `SandboxTemplate`,
`SandboxClaim`, or `SandboxWarmPool` resources.

## Cleanup

The cleanup script deletes the session Workloads, Helm release, namespace,
PVCs, generated Secrets, MinIO data, and standalone PriorityClass. It removes
the node label. It intentionally retains the host scratch mount because an
automatic unmount or file deletion cannot safely distinguish a reused mount
from one created solely for this evaluation.

```bash
projects/embervm/standalone/cleanup.sh
```

To inspect the retained host state:

```bash
findmnt --target /var/lib/embervm/scratch
grep -F '/var/lib/embervm/scratch.img /var/lib/embervm/scratch ext4 loop,defaults 0 0' /etc/fstab
sudo ls -lh /var/lib/embervm/scratch.img
```

Only if those commands identify the quickstart loop image and no other service
uses the mount, purge it explicitly:

```bash
sudo umount /var/lib/embervm/scratch
sudo sed -i '\|^/var/lib/embervm/scratch.img /var/lib/embervm/scratch ext4 loop,defaults 0 0$|d' /etc/fstab
sudo rm -- /var/lib/embervm/scratch.img
sudo rmdir --ignore-fail-on-non-empty /var/lib/embervm/scratch
```

The namespace PVC deletion and optional host purge are destructive. Preserve
the MinIO PVC or copy its data before cleanup if the evaluation artifacts are
needed later.

## Validation levels

`standalone/quickstart_test.py` checks fixture reproducibility, manifest
security and persistence fields, and the narrowed chart render. Those are
packaging checks. They do not prove KVM bootstrap, guest readiness, task
execution, bank, relight, or workspace continuity. A real acceptance record
must come from an x86_64 Linux host with working `/dev/kvm` and must include the
hello assertion plus the observed `banked` state and post-relight value
assertion shown above.
