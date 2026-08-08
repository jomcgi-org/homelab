# Shared Firecracker utilities

The original `fc-invoke` daemon is retired. This directory retains four
components used by EmberVM and its guest images:

| Directory | Role |
| --- | --- |
| `egress-proxy/` | Applies the guest egress allowlist and injects credentials outside the microVM. |
| `rootfs-builder/` | Exports OCI images into Firecracker root filesystems. |
| `shim/` | Shared in-guest HTTP server and capability hooks. |
| `vsockproto/` | Host/guest message types and vsock port constants. |

The current request flow, lifecycle, and security invariants are documented in
[`projects/embervm/ARCHITECTURE.md`](../../embervm/ARCHITECTURE.md).
