# Firecracker components

This directory contains guest images and shared utilities used by
[EmberVM](../embervm/). The original `fc-invoke` daemon and its deployment were
retired after workloads moved to EmberVM.

| Directory | Current role |
| --- | --- |
| [`sandbox/`](sandbox/) | Python sandbox guest retained for the EmberVM task and session runtimes. |
| [`semgrep/`](semgrep/) | Semgrep guest retained for EmberVM scan workloads. |
| [`substrate/`](substrate/) | Shared guest shim, wire types, egress proxy, and rootfs-builder image. |
| [`tools/`](tools/) | Generators shared by the guest images. |

The live orchestration design, workload lifecycle, security boundaries, and
fleet state are documented in
[`projects/embervm/ARCHITECTURE.md`](../embervm/ARCHITECTURE.md).
