This file describes the environment you are running in. It is generated at
image build time and always matches the installed packages exactly.

- You are inside a disposable Firecracker microVM. The rootfs is read-only,
  write scratch files under /tmp or /workspace.
- Outbound network goes through an egress proxy with an allowlist; most
  destinations are blocked. Do not assume general internet access.
- Runtimes available: python3 (with the scientific libraries listed below),
  node/pnpm, go. Prefer running real code over estimating results.
- matplotlib works headless (Agg). Save figures to files.
