# Apple Silicon development

Run the real EmberVM Firecracker driver locally inside a persistent Linux VM.
The host has 4 vCPUs and 10 GiB RAM. Each probe restores four ARM microVMs,
each with 1 vCPU and 1536 MiB RAM, from one shared snapshot.

This is the first development milestone of [#6132](https://github.com/jomcgi-org/homelab/issues/6132).
It proves cold boot, snapshot/restore, HTTP over vsock, separate guest memory
and tmpfs state, and teardown. It uses a tiny probe guest. The Semgrep warm
scan-server still needs an ARM artifact, and the task library and scan HTTP
service have not yet been extracted.

## Run

Prerequisites: an M3 or newer Apple Silicon Mac, macOS 15 or newer, Python 3,
and the Go version required by the repository's `go.mod`. This was exercised
on an M4 Pro with 24 GB RAM and macOS 26.6.2, using Lima 2.2.0.

```sh
brew install lima
python3 projects/embervm/dev/apple/dev.py run
```

Run from a dedicated worktree when editing code. The command:

1. Creates or starts `embervm-dev`, an ARM Ubuntu Linux host with nested KVM.
2. Cross-compiles the current worktree's host probe and guest for Linux ARM64.
3. Copies only the binaries and provisioning inputs into the Linux host.
4. Downloads checksum-verified Firecracker and guest-kernel artifacts.
5. Builds an ext4 rootfs containing the static probe guest as PID 1.
6. Cold-boots and snapshots the guest, or reuses its compatible cached base.
7. Restores four guests, writes distinct markers into their memory and tmpfs,
   reads each marker back while all four are alive, and destroys them.
8. Repeats with four fresh restores and verifies their initial state is empty.

`PASS` means both waves completed, each guest retained its own state, and the
driver reaped all guest processes and removed their bundles. The probe uses
the existing `noded/fcvm/driver` and `noded/vsockhttp` packages directly.
It does not run Bazel or a test suite on macOS.

The outer Linux host stays running between invocations. Rebuild by running the
same command again. Go's normal build cache is reused. A guest binary change
creates a new rootfs; changing the rootfs, kernel, Firecracker, sizing, paths,
or outer Linux boot ID creates a new base. Rebuilding only the host probe can
reuse the existing base. Each invocation still creates fresh microVMs.

```sh
python3 projects/embervm/dev/apple/dev.py status
python3 projects/embervm/dev/apple/dev.py stop
python3 projects/embervm/dev/apple/dev.py up
```

Use `--instance NAME` with every command to use a separate Lima host. Existing
instances with a different architecture, VM type, nested-virtualization setting,
memory, or CPU count are refused. Configuration changes to `lima.yaml` apply to
new instances; use a new name to validate an updated host configuration.

## Resources and limits

Four guests reserve `4 * (1536 + 192) + 512 = 7424 MiB` under the proposed
production budget. The 10 GiB outer host leaves space for Linux and preparation.
All four guest vCPUs share the outer host's four vCPUs with Firecracker and the
probe. This is development sizing, not a throughput or exclusive-core claim.
The tiny guest does not dirty its entire configured RAM, so these measurements
do not establish Semgrep's memory or latency requirements.

The probe intentionally runs with the jailer disabled and accepts only its
built-in state checks. It does not establish adversarial isolation, per-VM OOM
containment, CPU/PID limits, token handling, egress, or Kubernetes readiness.
The microVMs have no network interface. No Mac directories or SSH agent are
mounted/forwarded into the Linux host, and incidental guest ports are not
published. Do not use this probe to execute untrusted workloads.

The Firecracker URL and checksum come from `MODULE.bazel`, matching the
repository pin. The development kernel is an upstream Firecracker CI ARM
kernel pinned in `kernel.json`; it differs from the deployed Kata kernel.
ARM snapshots stay on this development host. The later amd64 Kubernetes
deployment must build and validate its own snapshots.

## Diagnostics and cleanup

The complete most recent run is retained inside Linux:

```sh
limactl shell --workdir=/ embervm-dev sudo cat /var/lib/embervm-dev/last-run.log
limactl shell --workdir=/ embervm-dev sudo pgrep -a firecracker
```

The second command should find no processes after a completed run. Failure
prints the log tail. Each probe has a three-minute overall timeout, and its
normal error and termination paths reap guests. Concurrent runs are refused.

Artifacts and snapshots live on the Linux disk under `/var/lib/embervm-dev`.
Old bases and rootfs versions are retained for debugging and can accumulate
as the guest changes. To reclaim the entire dedicated environment after
stopping it, including all caches and snapshots:

```sh
python3 projects/embervm/dev/apple/dev.py stop
limactl delete embervm-dev
```

Lima's initial image download and the first Go dependency download need network
access. Subsequent runs reuse cached inputs. If `/dev/kvm` is missing, confirm
the Mac supports nested virtualization and that the instance was created from
this template. The runner checks both the KVM API version and actual VM creation
before starting Firecracker.

## Next milestones

1. Supply an ARM experimental Semgrep scan-server artifact and prove its warm
   readiness and scanning inside this environment. Resolve token delivery to
   the pre-existing snapshotted scanner process and reseeding before TLS.
2. Extract the native Go task library, then add the HTTP scan service and bounded
   primed pool. Extend the same local command to scan fixtures, saturate the pool,
   cancel work, and drain cleanly.
3. Deploy one Kubernetes size class with OCI rootfs delivery into `emptyDir`,
   per-pod snapshots, probes, Guaranteed QoS, fail-closed admission and a completed
   jailer. Validate memory/CPU/PID containment before untrusted workloads.
4. Add capacity metrics, HPA, and additional size classes after measuring actual
   scanner parallelism and pod warm-up. Validate these on the target EKS nodes.

Platform references: [Apple nested virtualization](https://developer.apple.com/documentation/virtualization/vzgenericplatformconfiguration/isnestedvirtualizationsupported),
[Lima configuration](https://github.com/lima-vm/lima/blob/master/templates/default.yaml),
and [Firecracker prerequisites](https://github.com/firecracker-microvm/firecracker/blob/main/docs/getting-started.md).
