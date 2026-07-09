"""Semgrep experimental offline-Pro engine OCI artifact digest.

The experimental engine is an osemgrep-pro build that runs a warm stdio
"scan-server" (osemgrep-pro mcp --experimental --pro) fully offline: it warms
per-language parsers, prints one {"ready":true} line, then answers
newline-delimited JSON scanFiles requests. It is what the Firecracker semgrep
guest (projects/firecracker/semgrep) runs in place of the old resident
`semgrep lsp`, which could only ever run OSS analysis.

amd64 only: there is no arm64 engine build and no arm64 Firecracker nodes.

Pinned manually (not by the update-semgrep-pro workflow) when a new engine image
is published to ghcr.io/jomcgi/homelab/tools/semgrep-experimental/engine-<arch>.
"""

SEMGREP_EXPERIMENTAL_DIGESTS = {
    "engine_amd64": "sha256:a4c498cb309d3f97928d837c17a295cd108c4cce775e33fb0715a953c1fbf3b2",
}
