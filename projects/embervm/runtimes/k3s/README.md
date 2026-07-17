# EmberVM k3s guest images (R5 composite)

The composite (R5) guest substrate: two apko images, `k3s-server` and
`k3s-agent`, that run an airgapped k3s cluster inside Firecracker microVMs on the
kata `vmlinux.container` guest kernel (ADR embervm/001, plan Task 3). They are
the k8s-knowledge half of the rung: EmberVM injects only generic `EMBER_GROUP_*`
facts (standing decision 13) and these images map them to k3s flags themselves.

## The two images

Both images share one apko config, one PID-1 init binary
(`guest-init/cmd/ember-k3s-init`), and the SAME vendored airgap image tarball.
They differ only in name and repository; the role is data, read from
`EMBER_GROUP_ROLE`:

| Image        | Role   | k3s command                                             | Health port |
| ------------ | ------ | ------------------------------------------------------- | ----------- |
| `k3s-server` | server | `k3s server` (sqlite, flannel host-gw, group token)     | 6443 (API)  |
| `k3s-agent`  | agent  | `k3s agent --server https://$EMBER_PEER_SERVER:6443 ...` | 10250 (kubelet) |

For the Task 3 single-VM spike only `k3s-server` is drilled to Ready; the agent
image is built and test-covered but exercised under the group machinery in Task
5+.

## What is vendored (zero egress, standing decision 12)

Cluster bring-up pulls NOTHING. The `@k3s_archive` Bazel repo rule
(`bazel/tools/http/k3s_archive.bzl`, wired in `MODULE.bazel`) bakes, per arch:

- the static `k3s` binary at `/usr/local/bin/k3s`; and
- the airgap image tarball at
  `/var/lib/rancher/k3s/agent/images/k3s-airgap-images-<arch>.tar.zst`, which k3s
  auto-imports at startup (compressed `.tar.zst` is imported directly, no
  in-guest decompression step).

### Pinned k3s version and artifact sizes

| Artifact                               | amd64   | arm64   |
| -------------------------------------- | ------- | ------- |
| k3s version                            | v1.30.6+k3s1 | v1.30.6+k3s1 |
| k3s binary (`/usr/local/bin/k3s`)      | ~63 MiB | ~59 MiB |
| airgap tarball (`.tar.zst`, baked)     | ~133 MiB | ~120 MiB |

Checksums are the upstream release `sha256sum-<arch>.txt` values, verified
2026-07-17, pinned in `MODULE.bazel`. The arm64 binary is the upstream `k3s-arm64`
release asset; it lands at the same in-image path `/usr/local/bin/k3s`.

## In-guest ROOT posture (deliberate deviation, documented)

These images run as **root (uid 0) inside the guest**, deviating from the repo's
uid 65532 non-root convention. This is deliberate and stated here rather than
being silent: k3s (its embedded containerd, runc, the CNI plugins, iptables/nft
programming, cgroup mounts, and the kubelet) realistically requires root inside
the guest. The isolation statement is the **microVM boundary**, not an in-guest
uid: the task-class posture applies (a compromised guest is confined to its own
Firecracker VM with no egress beyond its group and the declared entry path). The
`apko.yaml` `run-as: 0` is honoured only on a non-Firecracker (docker/crane)
path; a raw Firecracker boot boots `init=/usr/local/bin/ember-k3s-init` as PID 1
regardless.

## Injected facts to k3s flags (standing decision 13)

The platform injects generic facts through the MMDS-lite boot-arg seam
(`ember.env.<KEY>=<base64url>`, decoded by the init into the process env). For the
Task 3 spike they arrive via the serving lane's `mmds_env`; under the group
machinery (Task 4+) via the identical seam. The init maps:

| Injected fact                | k3s use                                                      |
| ---------------------------- | ----------------------------------------------------------- |
| `EMBER_GROUP_ROLE`           | selects `k3s server` vs `k3s agent`                         |
| `EMBER_GROUP_SECRET`         | `--token` (cluster token) + the static token-auth API entry |
| `EMBER_GROUP_IP`             | `--node-ip` (and `--advertise-address` on the server)       |
| `EMBER_PEER_SERVER`          | the agent's `--server https://<ip>:6443`                    |

The server writes a static token-auth CSV (`/run/ember/token-auth.csv`, derived
from `EMBER_GROUP_SECRET`) and passes it via
`--kube-apiserver-arg=token-auth-file=...`, so the consumer's kubeconfig
authenticates with the same secret as a bearer token (scratch-tier posture, plan
Task 10). flannel uses the `host-gw` backend, which routes over the group's flat
L2 subnet with no vxlan kernel module (Fork 3).

## Sizing (fc-base coupling, live numbers TODO)

The airgap tarball import at first boot sets the memory high-water mark
(`reference_fc_base_build_sizing_coupling`): k3s decompresses and imports ~130 MiB
of images into containerd's snapshotter at startup, and the memMib is passed
per-request by the control plane (not auto-sized). The live single-VM spike
records the real floor; until it runs these are **TODO placeholders, not
asserted** (see `drill/README.md`):

| Metric                          | Value                    |
| ------------------------------- | ------------------------ |
| memory floor (server, boot-to-Ready) | TODO (spike)        |
| rootfs size (server image)      | TODO (spike / crane)     |
| airgap tarball size (baked)     | ~133 MiB amd64 / ~120 MiB arm64 (above) |
| boot-to-Ready (cold, zero egress) | TODO (spike)           |

The scratch-k8s consumer CR (plan Task 10) sizes members from these numbers; the
provisional spec is server 2 vCPU / 2048 MiB and agents 1 vCPU / 1024 MiB, to be
confirmed against the spike's floor.

## Kernel requirements (verified in-guest by the spike, not asserted here)

k3s needs `overlayfs`, `br_netfilter`, `veth`, `nf_conntrack`, and iptables/nft
from the guest kernel. The kata `vmlinux.container` kernel is container-shaped and
had `netfilter=y` patched in for kata, but the kernel is a static prebuilt binary
(`@kata_firecracker`), NOT built per apko config, so it is not patchable at Bazel
time in this task. The spike **verifies** these in-guest rather than asserting
them (`drill/README.md` has the exact `zcat /proc/config.gz | grep ...` command);
any gap found is recorded with its fix path (a newer kata kernel or a custom
kernel build), not solved here.

## Boot integration

A raw Firecracker boot ignores the OCI `entrypoint` and boots
`init=<HarnessInit>`, so the image ships a real PID 1: `ember-k3s-init`
(`guest-init/cmd/`, layered at `/usr/local/bin/ember-k3s-init` by the image
`BUILD`). On boot it mounts k3s's writable + pseudo filesystems (tmpfs over
`/run`, `/var/log`, `/tmp`; `/proc`, `/sys`, cgroup2), decodes the
`EMBER_GROUP_*` facts, starts the vsock guest control agent
(`noded/guestagent`) on the frozen port 1024 for post-resume clock resync
(standing decision 7), maps the facts to k3s flags, and supervises k3s. It is
`fork+exec` (not `exec`-replace) because the clock-agent goroutine must live
alongside k3s; this init is the supervisor.
