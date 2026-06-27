load("//bazel/tools/oci:go_image.bzl", "go_image")

# Egress-proxy sidecar (ADR 023, Task 6a.1) container image: an allowlisted HTTP
# forward proxy. Pushed to ghcr.io/jomcgi/homelab/projects/agent_platform/egress-proxy
# (derived from the package path by go_image).
go_image(
    name = "image",
    binary = "//projects/agent_platform/egress-proxy/cmd",
)
