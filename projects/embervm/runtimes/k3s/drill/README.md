# R5 Task 3 single-VM k3s spike drill

A **throwaway** drill (no composite/group machinery, plan Task 3 scope): boot one
`k3s-server` guest on deployed noded through the existing R3 serving lane, confirm
it reaches Kubernetes-Ready with zero egress, verify the guest kernel provides
what k3s needs, and record the sizing numbers. The controller runs this on the
cluster and fills the TODO placeholders into the PR description; nothing here is
fabricated.

The drill CR is `workload-k3s-spike.yaml` in this directory. It is applied by
hand and removed after; it is NOT in the deployed chart.

## Prerequisites

- The `k3s-server` image built and pushed by this PR's CI (the drill CR's
  `source.image.ref`). For a throwaway hand-drill `:latest` is fine; for a
  reproducible drill pin the CI-built digest.
- noded resolving the image: the ref must be in `EMBERVM_NODED_IMAGES` (add the
  k3s-server ref the same way the other runtime images are registered), or the
  drill boots with an image-not-found error.
- `kubectl` context for the homelab cluster (to apply the CR and read status),
  and `grpcurl` access to noded if driving the serving lane directly.

## Step 1: boot the guest through the serving lane

Apply the drill CR so noded cold-boots the k3s-server as a serving instance (tap
NIC + TCP health-gate on 6443):

```bash
kubectl apply -f projects/embervm/runtimes/k3s/drill/workload-k3s-spike.yaml
```

Watch the Workload reach a serving-ready condition and note the endpoint noded
publishes (the tap IP:port). Record wall-clock from apply to the serving lane's
TCP health-gate passing on 6443:

```bash
kubectl -n embervm get workload k3s-spike -o yaml | yq '.status'
# noded logs: the tap alloc, ClaimServing, and the ip:6443 health-gate transition
kubectl -n embervm logs ds/embervm-noded --since=10m | grep -iE 'k3s-spike|serving|health'
```

TCP-open on 6443 means the k3s API socket is up; it does NOT yet mean the node is
Kubernetes-Ready. Step 2 confirms Ready.

## Step 2: confirm the node reaches Kubernetes-Ready

Exec into the guest is not directly available (it is a microVM), so reach the API
over the published tap endpoint using the throwaway token from the CR
(`EMBER_GROUP_SECRET`), or read it from inside via the guest console logs.

From a pod that can reach the tap endpoint (e.g. a debug pod in the embervm ns),
with `SERVER=https://<tap-ip>:6443` and `TOKEN=spike-throwaway-token-do-not-reuse`:

```bash
kubectl --server="$SERVER" --token="$TOKEN" --insecure-skip-tls-verify get nodes -o wide
```

Expected: one node, STATUS `Ready`. Record wall-clock from Step 1's apply to the
first `Ready`. Paste the full `kubectl get nodes` output into the PR.

`kubectl get nodes` output (drill):

```
TODO: paste `kubectl get nodes -o wide` here (must show 1 node Ready)
```

## Step 3: verify zero egress during bring-up

The airgap tarball means bring-up pulls nothing. Confirm no image-pull egress left
the guest during Step 1 to Step 2:

```bash
# On the node, watch the guest's tap device counters / any egress-proxy activity
# for the k3s-spike instance during boot; expect image-registry traffic == 0.
# The guest has no egress NIC beyond the tap by construction (task-class posture).
```

Record: egress observed during bring-up (expected: none). Confirm k3s imported
the baked airgap tarball (guest console log: `Imported images from
/var/lib/rancher/k3s/agent/images/...`).

## Step 4: verify the guest kernel provides k3s's needs

The kata `vmlinux.container` kernel is a static prebuilt binary
(`@kata_firecracker`), NOT built per apko config, so its config is not patchable
at Bazel time in this task. **Verify, do not assume.** From inside the guest (or
via a console command baked for the drill), check the kernel config:

```bash
zcat /proc/config.gz | grep -E 'OVERLAY_FS|BRIDGE_NETFILTER|VETH|NF_CONNTRACK|NETFILTER_XT|IP_NF_IPTABLES|NF_TABLES'
```

Expected (each `=y` or `=m`):

- `CONFIG_OVERLAY_FS` (containerd overlayfs snapshotter)
- `CONFIG_BRIDGE_NETFILTER` (br_netfilter, for kube-proxy/flannel)
- `CONFIG_VETH` (CNI veth pairs)
- `CONFIG_NF_CONNTRACK` (kube-proxy conntrack)
- `CONFIG_NETFILTER_XTABLES` / `CONFIG_IP_NF_IPTABLES` and/or `CONFIG_NF_TABLES`
  (iptables/nft dataplane)

If `/proc/config.gz` is absent, fall back to run-time probes: `lsmod`,
`modprobe overlay` / `br_netfilter`, and `k3s check-config` (k3s ships this
subcommand, which reports the exact missing options).

Kernel-config verification result (drill):

```
TODO: paste the `zcat /proc/config.gz | grep ...` (or `k3s check-config`) output.
For any option NOT present, record the fix path (a newer kata kernel, or a custom
kernel build via the apko-config-checksum-patch seam) rather than asserting it holds.
```

**Assumptions this PR could NOT verify from the build host** (the live drill
resolves each):

- `CONFIG_OVERLAY_FS` present in kata 3.32.0 `vmlinux.container`.
- `CONFIG_BRIDGE_NETFILTER` (br_netfilter) present. Kata patched `netfilter=y`,
  but `BRIDGE_NETFILTER` specifically must be confirmed.
- `CONFIG_VETH`, `CONFIG_NF_CONNTRACK`, `CONFIG_NF_TABLES` / iptables backend
  present.
- cgroup2 mountable at `/sys/fs/cgroup` in the guest (the init mounts it
  best-effort; kubelet needs it).

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

Confirm noded tears the instance and tap down and the endpoint is unpublished.

## What the PR description must contain (acceptance)

- boot-to-Ready, memory floor, rootfs size (Step 5).
- the `kubectl get nodes` output showing the node Ready (Step 2).
- the kernel-config verification result, with a recorded fix path for any gap
  (Step 4).
- confirmation of zero egress during bring-up (Step 3).
