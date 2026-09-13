# EmberVM single-host quickstart

This walkthrough installs the published EmberVM Helm chart on one x86-64 Linux
host, starts one control plane and one `noded`, runs a Python hello-world task,
and exercises a session through create, invoke, automatic suspend, relight with
workspace continuity, inspection, and deletion.

It is independent of the homelab reference deployment. It does not use its k3s
fleet shape, SeaweedFS, 1Password Operator, Cloudflare Tunnel, Longhorn, or
SigNoz. Kubernetes remains part of EmberVM's supported platform contract: the
single machine runs k3s and both EmberVM components.

## Support boundary and prerequisites

Use a disposable or otherwise dedicated Linux machine. The walkthrough creates
a k3s cluster, a cluster-scoped CRD and RBAC objects, a PriorityClass, and files
under `/var/lib/embervm/scratch`.

- x86-64 Linux with systemd and KVM exposed as `/dev/kvm`. A cloud VM works only
  when its provider exposes nested virtualization. macOS, Windows, WSL, and an
  ARM host are not supported by the current control-plane and noded images.
- At least 4 vCPU, 8 GiB RAM, and 25 GiB free disk. The profile gives noded a
  3 GiB memory limit, the Python guest 512 MiB, the control plane 512 MiB, and
  creates one sparse 4 GiB base rootfs plus snapshots on scratch.
- `curl`, `git`, `jq`, Helm 3.8 or newer, and a checkout of this repository at
  the revision containing this guide. Run the commands from the checkout root.
- Outbound HTTPS access to the k3s installer and `ghcr.io`.
- A GitHub account authorized to pull this repository's private container
  packages, plus a classic personal access token with `read:packages`. The Helm
  chart is anonymously readable, but its component and guest images currently
  are not. If your account has not been granted package access, stop here: there
  is no supported anonymous image or source-build bootstrap in this quickstart.

The commands below read the matching EmberVM chart version from this checkout.
Do not combine the standalone overlay with `deploy/values.yaml` or
`dev/deploy/values.yaml`; those are homelab environments, not prerequisites.

## 1. Verify the host

```bash
test "$(uname -m)" = x86_64
test -c /dev/kvm
sudo test -r /dev/kvm
sudo test -w /dev/kvm
grep -Eq '(^| )vmx( |$)|(^| )svm( |$)' /proc/cpuinfo
df -h /
```

If `/dev/kvm` is absent, enable virtualization in firmware or choose a machine
that exposes nested virtualization. EmberVM cannot fall back to software
emulation.

## 2. Install a one-node Kubernetes cluster

This pins the k3s release used when this guide was written and disables Traefik,
which the task/session walkthrough does not use.

```bash
export INSTALL_K3S_VERSION=v1.36.4+k3s1
curl -sfL https://get.k3s.io | INSTALL_K3S_EXEC="--disable traefik" sh -

sudo install -o "$(id -u)" -g "$(id -g)" -m 0600 \
  /etc/rancher/k3s/k3s.yaml "$PWD/embervm-kubeconfig"
export KUBECONFIG="$PWD/embervm-kubeconfig"

kubectl wait --for=condition=Ready node --all --timeout=2m
export EMBER_NODE="$(kubectl get nodes -o jsonpath='{.items[0].metadata.name}')"
test "$(kubectl get nodes --no-headers | wc -l)" -eq 1
kubectl label node "$EMBER_NODE" embervm.jomcgi.dev/node=true
sudo install -d -m 0755 /var/lib/embervm/scratch
```

The quickstart uses k3s's `local-path` StorageClass for the SQLite op-log. The
scratch directory is local to this host. For a long-lived installation, mount a
dedicated filesystem there before installing EmberVM.

## 3. Supply registry access and install EmberVM

Export the token in your shell, not in a values file. The commands create a
normal Kubernetes pull Secret and tell the chart to use it; no secret operator
is involved.

```bash
export GHCR_USERNAME=your-github-login
export GHCR_TOKEN=your-read-packages-token
export EMBER_CHART_VERSION="$(awk '$1 == "version:" {print $2; exit}' \
  projects/embervm/chart/Chart.yaml)"
test -n "$EMBER_CHART_VERSION"

kubectl create namespace embervm
kubectl -n embervm create secret docker-registry embervm-quickstart-registry \
  --docker-server=ghcr.io \
  --docker-username="$GHCR_USERNAME" \
  --docker-password="$GHCR_TOKEN"
kubectl -n embervm create serviceaccount quickstart-client

kubectl apply -f - <<'EOF'
apiVersion: scheduling.k8s.io/v1
kind: PriorityClass
metadata:
  name: embervm-quickstart
value: -10
globalDefault: false
description: Disposable Firecracker capacity for the EmberVM quickstart.
EOF

helm upgrade --install embervm \
  oci://ghcr.io/jomcgi/homelab/charts/embervm \
  --version "$EMBER_CHART_VERSION" \
  --namespace embervm \
  --values projects/embervm/chart/standalone-values.yaml \
  --wait \
  --timeout 20m
```

Image extraction and the first Firecracker base build can take several minutes.
The profile builds only `sandbox-python`; it does not pull or prepare the agent,
browser, compiler, database, serving, or egress lanes.

## 4. Wait for component and workload readiness

```bash
kubectl -n embervm rollout status deployment/embervm-embervm --timeout=5m
kubectl -n embervm rollout status daemonset/embervm-embervm-noded --timeout=15m
kubectl -n embervm wait workload/sandbox-python \
  --for='jsonpath={.status.conditions[?(@.type=="Ready")].status}=True' \
  --timeout=15m

kubectl -n embervm get pods -o wide
kubectl -n embervm get workloads
kubectl -n embervm get workload sandbox-python -o yaml
```

Create a one-hour management token and forward the in-cluster control-plane
Service. The management token is a Kubernetes ServiceAccount token reviewed by
the control plane. A session token created later is a different, single-session
capability and must not be substituted for it.

```bash
export EMBER_TOKEN="$(kubectl -n embervm create token quickstart-client --duration=1h)"
kubectl -n embervm port-forward service/embervm-embervm 18080:8080 \
  >"$PWD/embervm-port-forward.log" 2>&1 &
export EMBER_PORT_FORWARD_PID=$!
export EMBER_URL=http://127.0.0.1:18080

curl -fsS "$EMBER_URL/healthz"
curl -fsS -H "Authorization: Bearer $EMBER_TOKEN" \
  "$EMBER_URL/v1/nodes" | jq .
```

The node response must contain one `dispatchable: true` node and a ready
`sandbox-python` workload fact before continuing.

## 5. Run hello world

Task submission is a native control-plane operation. `wait=true` returns the
guest response directly after the disposable microVM exits.

```bash
curl -fsS \
  -H "Authorization: Bearer $EMBER_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"code":"print(\"hello from EmberVM\")"}' \
  "$EMBER_URL/v1/workloads/sandbox-python/tasks?wait=true" \
  | tee "$PWD/embervm-hello.json"

jq -e '.exit_code == 0 and (.stdout | contains("hello from EmberVM"))' \
  "$PWD/embervm-hello.json"
```

This task uses the chart-registered `sandbox-python` Workload and the pinned
guest image already prepared by noded. No pod is created per task.

## 6. Register a session workload

The same shipped Python guest also implements the session guest contract. Read
its immutable image reference from the ready task Workload, then register it as
a session with a ten-second idle-bank policy. The control plane builds a
separate base because class and lifecycle settings participate in its signature.

```bash
export EMBER_SANDBOX_IMAGE="$(kubectl -n embervm get workload sandbox-python \
  -o jsonpath='{.spec.source.image.ref}')"

envsubst <<EOF | kubectl apply -f -
apiVersion: embervm.dev/v1alpha1
kind: Workload
metadata:
  name: quickstart-session
  namespace: embervm
spec:
  class: session
  source:
    image:
      ref: ${EMBER_SANDBOX_IMAGE}
      port: 1027
      readyPath: /shim/ready
      invokePath: /invoke
  resources:
    vcpus: 1
    memMib: 512
  concurrency:
    floor: 0
    cap: 1
  session:
    idleBankSeconds: 10
    maxLifetimeSeconds: 3600
    bankedTtlSeconds: 600
    maxSessions: 2
    invokeQueueCap: 1
  invocation:
    timeoutSeconds: 30
EOF

kubectl -n embervm wait workload/quickstart-session \
  --for='jsonpath={.status.conditions[?(@.type=="Ready")].status}=True' \
  --timeout=10m
kubectl -n embervm get workload quickstart-session -o yaml
```

`envsubst` is supplied by the `gettext` package on common distributions. If it
is unavailable, replace `${EMBER_SANDBOX_IMAGE}` manually with the exact output
of the preceding `kubectl get` command.

## 7. Create, execute, suspend, and resume

Create returns the session token exactly once. Keep it in the shell for this
walkthrough. Use the management token for create/delete and the session token
for invoke and status.

```bash
curl -fsS \
  -H "Authorization: Bearer $EMBER_TOKEN" \
  -H 'Idempotency-Key: embervm-quickstart-session-1' \
  --data '{}' \
  "$EMBER_URL/v1/workloads/quickstart-session/sessions" \
  | tee "$PWD/embervm-session-create.json"

export EMBER_SESSION_ID="$(jq -er .session_id "$PWD/embervm-session-create.json")"
export EMBER_SESSION_TOKEN="$(jq -er .session_token "$PWD/embervm-session-create.json")"

curl -fsS -H "Authorization: Bearer $EMBER_SESSION_TOKEN" \
  "$EMBER_URL/v1/sessions/$EMBER_SESSION_ID" | jq .
```

Execute Python that writes outside the sandbox handler's disposable per-invoke
directory. `/tmp` is guest memory and therefore part of this memory-bank
session's snapshot.

```bash
curl -fsS \
  -H "Authorization: Bearer $EMBER_SESSION_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"code":"from pathlib import Path; Path(\"/tmp/embervm-marker\").write_text(\"workspace survived\\n\"); print(\"marker written\")"}' \
  "$EMBER_URL/v1/sessions/$EMBER_SESSION_ID/invoke" \
  | tee "$PWD/embervm-session-write.json"

jq -e '.exit_code == 0 and (.stdout | contains("marker written"))' \
  "$PWD/embervm-session-write.json"
```

Suspend is automatic, not a public `suspend` command. After
`idleBankSeconds`, the 30-second session sweep asks noded to snapshot process
memory and tear down the VM. Poll the shipped status API until it reports
`banked`:

```bash
for attempt in $(seq 1 90); do
  state="$(curl -fsS -H "Authorization: Bearer $EMBER_SESSION_TOKEN" \
    "$EMBER_URL/v1/sessions/$EMBER_SESSION_ID" | jq -r .state)"
  printf 'session state: %s\n' "$state"
  test "$state" = banked && break
  sleep 1
done
test "$state" = banked
```

Invoke again. An invoke against `banked` is the native resume operation: the
control plane relights the snapshot, noded resynchronizes the restored guest
clock, and the guest reads the marker written before suspension.

```bash
curl -fsS \
  -H "Authorization: Bearer $EMBER_SESSION_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"code":"from pathlib import Path; print(Path(\"/tmp/embervm-marker\").read_text(), end=\"\")"}' \
  "$EMBER_URL/v1/sessions/$EMBER_SESSION_ID/invoke" \
  | tee "$PWD/embervm-session-resume.json"

jq -e '.exit_code == 0 and (.stdout == "workspace survived\n")' \
  "$PWD/embervm-session-resume.json"
curl -fsS -H "Authorization: Bearer $EMBER_SESSION_TOKEN" \
  "$EMBER_URL/v1/sessions/$EMBER_SESSION_ID" | jq .
```

That assertion is the continuity check. It proves the second operation ran in
the restored session state, not a fresh disposable task guest.

## 8. Inspect logs and delete resources

There is no standalone EmberVM CLI and no per-session log API. Status uses the
native HTTP and Workload interfaces; operator logs use Kubernetes:

```bash
kubectl -n embervm logs deployment/embervm-embervm -c control-plane --since=10m
kubectl -n embervm logs daemonset/embervm-embervm-noded -c noded --since=10m
kubectl -n embervm get workload quickstart-session -o yaml
```

Delete the session with management auth, then delete its definition:

```bash
curl -fsS -X DELETE -H "Authorization: Bearer $EMBER_TOKEN" \
  "$EMBER_URL/v1/sessions/$EMBER_SESSION_ID" | jq .
kubectl -n embervm delete workload quickstart-session
```

## Error catalogue

The ordinary caller should use HTTP status, `reason`, and `retryable`. The
operator should then inspect the Workload condition and the control/noded logs.

| Failure | Caller evidence | Operator action |
| --- | --- | --- |
| No capacity | Session create returns `429` with `reason: no_capacity`, `workload_cap`, or `session_cap`; a class-wide hard wall can return `503` with `reason: fleet_full`. | Check `/v1/nodes` for `dispatchable`, memory headroom, drain state, and ready workload facts. Delete an abandoned session or add correctly sized capacity. |
| Image preparation | Workload `Ready=False`, normally `reason: BaseNotBuilt`; no `snapshotRef` is published. | Inspect the noded init container first, then control-plane logs. Confirm the pinned guest package can be pulled and scratch has at least 6 GiB free for a 4 GiB build plus staging. |
| Guest readiness | Workload remains `Ready=False`; the condition message or control log includes `guest readiness` and the `/shim/ready` failure. | Inspect noded logs and the guest-image contract. Correct `readyPath`, port, init binary, memory, or boot timeout. A broken guest never becomes a ready base. |
| Unsupported capability | Creating a session on the task-class `sandbox-python` Workload returns `403` with `reason: not_session_class`. Lower noded surfaces use explicit gRPC `Unimplemented` errors for disabled capabilities. | Use a Workload class and source supported by the guest. Do not reinterpret this as capacity or retry it unchanged. |

The unsupported-class response is safe to observe directly:

```bash
curl -sS -o "$PWD/embervm-unsupported.json" -w '%{http_code}\n' \
  -H "Authorization: Bearer $EMBER_TOKEN" --data '{}' \
  "$EMBER_URL/v1/workloads/sandbox-python/sessions"
jq . "$PWD/embervm-unsupported.json"
```

## Operator diagnostics

Keep these checks separate from the workload flow. They answer whether the
platform contract is present, not whether a caller formed a valid operation.

```bash
# Host virtualization
ls -l /dev/kvm
sudo test -r /dev/kvm -a -w /dev/kvm

# Local scratch and op-log storage
findmnt -T /var/lib/embervm/scratch || true
df -h /var/lib/embervm/scratch
kubectl -n embervm get pvc,pods

# Component readiness and the node's reported capacity
kubectl -n embervm get deployment/embervm-embervm daemonset/embervm-embervm-noded
kubectl -n embervm describe daemonset/embervm-embervm-noded
curl -fsS "$EMBER_URL/healthz"
curl -fsS -H "Authorization: Bearer $EMBER_TOKEN" \
  "$EMBER_URL/v1/nodes" | jq .

# Recent component logs
kubectl -n embervm logs deployment/embervm-embervm -c control-plane --since=15m
kubectl -n embervm logs daemonset/embervm-embervm-noded -c noded --since=15m
```

The minimal profile deliberately sets `noded.store.endpoint` to empty. Banked
session state is therefore local to `/var/lib/embervm/scratch`: it survives the
bank/relight shown above and ordinary control-plane restarts, but it is not a
node-loss recovery guarantee. Removing the host, losing scratch, or moving the
session to another CPU vendor loses that local-only continuity.

For off-node durability, provide an S3-compatible endpoint and bucket plus a
normal Kubernetes Secret through `noded.store.credentials`; then test network
reachability from the cluster before trusting exports. A generic reachability
probe, where `403` still proves the endpoint answered, is:

```bash
export EMBER_STORE_ENDPOINT=https://your-s3-endpoint.example
kubectl -n embervm run embervm-store-check --restart=Never \
  --image=curlimages/curl -- \
  sh -c 'code=$(curl -sS -o /dev/null -w "%{http_code}" "$0/"); test "$code" != 000' \
  "$EMBER_STORE_ENDPOINT"
kubectl -n embervm logs pod/embervm-store-check
kubectl -n embervm delete pod embervm-store-check
```

Endpoint reachability alone does not validate bucket permissions, SigV4
credentials, export completion, encryption, or restore. Those require a bank,
confirmed export logs, and a deliberate restore drill.

## Cleanup

Delete the namespaced release before removing cluster-scoped support objects and
host files:

```bash
kill "$EMBER_PORT_FORWARD_PID" 2>/dev/null || true
helm uninstall embervm --namespace embervm
kubectl delete namespace embervm --wait=true
kubectl delete priorityclass embervm-quickstart
kubectl delete crd workloads.embervm.dev
sudo rm -rf /var/lib/embervm/scratch/embervm-noded
sudo /usr/local/bin/k3s-uninstall.sh
rm -f "$PWD/embervm-kubeconfig" \
  "$PWD/embervm-port-forward.log" \
  "$PWD/embervm-hello.json" \
  "$PWD/embervm-session-create.json" \
  "$PWD/embervm-session-write.json" \
  "$PWD/embervm-session-resume.json" \
  "$PWD/embervm-unsupported.json"
unset GHCR_TOKEN EMBER_TOKEN EMBER_SESSION_TOKEN
```

The scratch cleanup is intentionally limited to EmberVM's own
`/var/lib/embervm/scratch/embervm-noded` subtree. It does not remove the scratch
mount or unrelated host data.

## Compatibility and validation status

This is the native EmberVM API, not the Kubernetes Agent Sandbox API. EmberVM
does not currently ship an Agent Sandbox adapter, and no adapter is required by
this quickstart. The compatibility gate and its still-open review are tracked in
[#5806](https://github.com/jomcgi-org/homelab/issues/5806).

The chart/overlay combination is regression-tested by rendering the real Helm
templates and asserting that it contains one control plane, one noded, one
Python Workload, local-path storage, no fc-agentd, and none of the homelab-only
services. The published `0.78.2` chart was also confirmed anonymously readable
from GHCR while this guide was written; its images returned authorization
failures without package credentials, which is why registry access is an
explicit prerequisite above.

The complete KVM lifecycle was not executed in the environment that authored
this guide because it exposes no `/dev/kvm`, `kubectl`, or pre-existing Linux
cluster, and this task does not authorize deploying to another environment.
Run every assertion above on a clean disposable Linux host before treating the
quickstart as operationally validated. The repository's required Linux CI is a
static and build gate, not evidence of a production cutover or a KVM drill.
