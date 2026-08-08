# Semgrep guest

This directory builds the Semgrep guest image used by EmberVM scan workloads.
The image keeps the Pro engine and rules offline, starts the resident scan
server, and exposes the shared vsock request contract through its PID 1 guest
harness.

The image is pinned into `projects/embervm/chart/BUILD` as
`semgrep.guestImage`. VM lifecycle, snapshot restore, scheduling, and deployment
belong to EmberVM and are documented in
[`projects/embervm/ARCHITECTURE.md`](../../embervm/ARCHITECTURE.md).
