# R5 Task 3 single-VM k3s spike drill

A **throwaway** drill (no composite/group machinery, plan Task 3 scope): boot one
`k3s-server` guest on deployed noded, confirm it reaches Kubernetes-Ready with
zero egress, verify the guest kernel provides what k3s needs, and record the
sizing numbers. The controller runs this on the cluster and fills the TODO
placeholders into the PR description; nothing here is fabricated.

The drill CR is `workload-k3s-spike.yaml` in this directory. It is applied by
hand and removed after; it is NOT in the deployed chart.

## Lane rationale (why STATEFUL, not serving)

The drill boots the k3s-server as a **`class: stateful`** workload, not serving.
This was verified against the control plane on origin/main:

- The **serving lane cannot boot a full k3s image**. Serving cold-boot resolves a
  `serving_image_ref` that names a base-built handler ARTIFACT (drive 2, the zip
  lane, D-R3.11.2) and imports the handler before serving; a full k3s image is
  not that shape. Serving also has NO env-injection seam (`spec.env` is not a CRD
  field and is pruned; `StartServingRequest` has no `mmds_env`).
- The **stateful lane is the exact precedent** (scratch-postgres) for a full
  non-shim image: it cold-boots the runtime rootfs with no drive-2 handler,
  attaches a writable volume, health-gates a TCP connect to the guest port over
  the tap NIC, and (when a `secretRef` is set) injects first-boot facts via
  `mmds_env` over boot-args. k3s's sqlite/kubelet state lives on the volume,
  which is a natural fit.

The base build (BuildBase) for either lane health-gates the guest on the frozen
vsock readiness contract (`GET /shim/ready` on `GuestHTTPPort` 1027); the k3s
guest-init answers it (mirroring scratch-postgres) so the base snapshot completes
and the image becomes cold-bootable.

## Factless single-server (no injected facts)

The spike proves ONE k3s server reaches Ready with NO injected env. The
guest-init defaults to the server role when `EMBER_GROUP_ROLE` is unset, and k3s
auto-generates its own cluster token when `EMBER_GROUP_SECRET` is unset (correct
for a single-server cluster with no peers). So the drill CR carries **no
`secretRef`**. Multi-node fact injection (roles, the peer map, a shared secret
mapped to `K3S_TOKEN`/`K3S_URL`, standing decision 13) is a MULTI-NODE concern
exercised later via **Task 5's `StartGroupMember` FRESH env seam**, and is
deliberately out of spike scope.

## Prerequisites

- The `k3s-server` image built and pushed by this PR's CI (the drill CR's
  `source.image.ref`). Pin the CI-built digest for a reproducible drill.
- noded resolving the image: the ref must be in `EMBERVM_NODED_IMAGES` (register
  the k3s-server ref the same way the other runtime images are), or the base
  build fails image-not-found.
- `kubectl` context for the homelab cluster (to apply the CR and read status),
  and `grpcurl` access to noded if driving the stateful lane directly.
- A free port in the chart's stateful listener range (default 5400-5409) for
  `stateful.listenPort` (the drill uses 5409).

## Step 1: boot the guest on the stateful lane

Apply the drill CR so noded base-builds then cold-boots the k3s-server as a
stateful instance (writable volume + tap NIC + TCP health-gate on 6443):

```bash
kubectl apply -f projects/embervm/runtimes/k3s/drill/workload-k3s-spike.yaml
```

Watch the Workload reach a ready condition and note the endpoint noded publishes
(cluster-internal `embervm-serving:<listenPort>`). Record wall-clock from apply
to the stateful lane's TCP health-gate passing on 6443:

```bash
kubectl -n embervm get workload k3s-spike -o yaml | yq '.status'
# noded logs: base build /shim/ready gate, then the cold boot: volume mkfs+mount,
# airgap staging, ClaimStateful, and the ip:6443 TCP health-gate transition.
kubectl -n embervm logs ds/embervm-noded --since=15m | grep -iE 'k3s-spike|stateful|health|airgap|ready'
```

TCP-open on 6443 means the k3s API socket is up; it does NOT yet mean the node is
Kubernetes-Ready. Step 2 confirms Ready.

## Step 2: confirm the node reaches Kubernetes-Ready

Reach the API over the published cluster-internal endpoint
(`embervm-serving:5409`). A factless server auto-generated its token, so read the
admin kubeconfig from inside the guest data dir (`/etc/rancher/k3s/k3s.yaml`) via
the guest console logs, or exec a client that trusts the cluster CA. From a pod
that can reach the endpoint:

```bash
# With the server-issued admin kubeconfig (auto-generated; server URL rewritten
# to the cluster-internal endpoint):
kubectl --kubeconfig ./k3s-spike.kubeconfig get nodes -o wide
```

Expected: one node, STATUS `Ready`. Record wall-clock from Step 1's apply to the
first `Ready`. Paste the full `kubectl get nodes` output into the PR.

`kubectl get nodes` output (drill):

```
TODO: paste `kubectl get nodes -o wide` here (must show 1 node Ready)
```

## Step 3: verify zero egress during bring-up

The airgap tarball means bring-up pulls nothing. Confirm no image-pull egress
left the guest during Step 1 to Step 2:

```bash
# On the node, watch the guest's tap device counters / any egress-proxy activity
# for the k3s-spike instance during boot; expect image-registry traffic == 0.
# The guest has no egress NIC beyond the tap by construction (task-class posture).
```

Record: egress observed during bring-up (expected: none). Confirm k3s imported
the staged airgap tarball (guest console log: `Imported images from
/var/lib/rancher/k3s/agent/images/...`, staged there by guest-init from the baked
`/opt/k3s-airgap`).

## Step 4: verify the guest kernel provides k3s's needs

The kata `vmlinux.container` kernel is a static prebuilt binary
(`@kata_firecracker`), NOT built per apko config, so its config is not patchable
at Bazel time in this task. **Verify, do not assume.** From inside the guest (or
via a console command baked for the drill), check the kernel config:

```bash
zcat /proc/config.gz | grep -E 'OVERLAY_FS|BRIDGE_NETFILTER|VETH|NF_CONNTRACK|NETFILTER_XT|IP_NF_IPTABLES|NF_TABLES|CGROUP'
```

Expected (each `=y` or `=m`):

- `CONFIG_OVERLAY_FS` (containerd overlayfs snapshotter)
- `CONFIG_BRIDGE_NETFILTER` (br_netfilter, for kube-proxy/flannel)
- `CONFIG_VETH` (CNI veth pairs)
- `CONFIG_NF_CONNTRACK` (kube-proxy conntrack)
- `CONFIG_NETFILTER_XTABLES` / `CONFIG_IP_NF_IPTABLES` and/or `CONFIG_NF_TABLES`
  (iptables/nft dataplane)
- `CONFIG_CGROUPS` + `CONFIG_CGROUP_*` (kubelet cgroup accounting)

**cgroup2 check (review note I1).** The guest-init mounts cgroup2 at
`/sys/fs/cgroup` best-effort. Confirm the kubelet actually has a usable cgroup2
hierarchy after boot, because a silently-failed cgroup2 mount leaves the kubelet
without controllers and it never goes Ready:

```bash
# Inside the guest:
stat -fc %T /sys/fs/cgroup           # expect: cgroup2fs
cat /sys/fs/cgroup/cgroup.controllers # expect: a non-empty controller list
                                      # (cpu memory io pids ...)
mount | grep cgroup                   # expect exactly one cgroup2 mount, no v1 leftovers
k3s check-config 2>&1 | grep -iE 'cgroup|overlay|conntrack|bridge'
```

If `/sys/fs/cgroup/cgroup.controllers` is empty or the mount is missing, the
guest-init cgroup2 mount did not take (or the kernel booted a hybrid/v1
hierarchy); record it as a gap with its fix path (mount cgroup2 explicitly with
`nsdelegate`, or add the kernel cgroup2 boot params) rather than asserting Ready.

Kernel-config + cgroup2 verification result (drill):

```
TODO: paste the `zcat /proc/config.gz | grep ...`, the cgroup2 checks, and
`k3s check-config` output. For any option NOT present or any cgroup2 gap, record
the fix path (a newer kata kernel, a custom kernel build via the
apko-config-checksum-patch seam, or an explicit cgroup2 mount) rather than
asserting it holds.
```

**Assumptions this PR could NOT verify from the build host** (the live drill
resolves each):

- `CONFIG_OVERLAY_FS` present in kata 3.32.0 `vmlinux.container`.
- `CONFIG_BRIDGE_NETFILTER` (br_netfilter) present. Kata patched `netfilter=y`,
  but `BRIDGE_NETFILTER` specifically must be confirmed.
- `CONFIG_VETH`, `CONFIG_NF_CONNTRACK`, `CONFIG_NF_TABLES` / iptables backend
  present.
- **cgroup2 mountable at `/sys/fs/cgroup` with a non-empty controller set** (the
  init mounts it best-effort; kubelet needs the controllers, see I1 above).

## Step 5: record the sizing numbers (fc-base coupling)

The airgap import at first boot sets the memory high-water mark. Record:

| Metric                                | Value (drill)          |
| ------------------------------------- | ---------------------- |
| boot-to-Ready (cold, zero egress)     | TODO                   |
| memory floor (min memMib that reaches Ready without OOM) | TODO |
| observed peak RSS during airgap import | TODO                  |
| rootfs size (server image, uncompressed) | TODO (`crane` below) |
| airgap tarball size (baked)           | ~133 MiB amd64 / ~120 MiB arm64 (from MODULE.bazel pins) |

Rootfs size from the published image:

```bash
crane export ghcr.io/jomcgi/homelab/projects/embervm/runtimes/k3s/k3s-server:latest - \
  | tar -tvf - | awk '{s+=$3} END {printf "%.0f MiB\n", s/1024/1024}'
```

To find the memory floor, re-run the drill lowering `resources.memMib` (2048 ->
1536 -> 1024) until k3s no longer reaches Ready (OOM during airgap import), and
record the lowest value that succeeds plus headroom.

## Step 6: teardown

```bash
kubectl delete -f projects/embervm/runtimes/k3s/drill/workload-k3s-spike.yaml
```

Confirm noded tears the instance, tap, and volume down and the endpoint is
unpublished. (The stateful lane's volume is GC'd per the CR's TTL; for a
throwaway drill delete the volume file too if it lingers.)

## What the PR description must contain (acceptance)

- boot-to-Ready, memory floor, rootfs size (Step 5).
- the `kubectl get nodes` output showing the node Ready (Step 2).
- the kernel-config + cgroup2 verification result, with a recorded fix path for
  any gap (Step 4).
- confirmation of zero egress during bring-up (Step 3).
