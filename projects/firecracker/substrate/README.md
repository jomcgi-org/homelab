# Shared Firecracker utilities

The original `fc-invoke` daemon is retired. This directory retains four
components used by EmberVM and its guest images:

| Directory | Role |
| --- | --- |
| `egress-proxy/` | Applies the guest egress allowlist and injects credentials outside the microVM. |
| `rootfs-builder/` | Exports OCI images into Firecracker root filesystems. |
| `shim/` | Shared in-guest HTTP server (/invoke dispatch and the /shim/* control surface). |
| `vsockproto/` | Vsock port constants shared by the guest-init binaries. |

The current request flow, lifecycle, and security invariants are documented in
[`projects/embervm/ARCHITECTURE.md`](../../embervm/ARCHITECTURE.md).
