# Shared Firecracker utilities

The original `fc-invoke` daemon is retired. This directory retains shared
components used by EmberVM and its guest images:

| Directory | Role |
| --- | --- |
| `bootenv/` | Boot-argument env codec shared by guest-init binaries. |
| `egress-proxy/` | Applies the guest egress allowlist and injects credentials outside the microVM. |
| `guestvolume/` | Linux guest volume mount helper shared by the Postgres and Python guest-inits. |
| `guestvsock/` | Linux guest listener and accepted-file adapters, shared by Ping, Postgres, k3s, Shotter and the sandbox. |
| `rootfs-builder/` | Exports OCI images into Firecracker root filesystems. |
| `shim/` | Shared in-guest HTTP server (/invoke dispatch and the /shim/* control surface). |
| `vsockproto/` | Vsock port constants shared by the guest-init binaries. |

The current request flow, lifecycle, and security invariants are documented in
[`projects/embervm/ARCHITECTURE.md`](../../embervm/ARCHITECTURE.md).

`guestvsock` preserves runtime diagnostics, accepted-file labels and descriptor
ownership. It adds no accept-unblocking, idempotent-close or deadline guarantees.
Shotter retains its context-aware dialer and dial-only connection adapter.
Injected socket tests and local socket-pair I/O tests cover the listener contract;
they do not establish live guest vsock operation.
