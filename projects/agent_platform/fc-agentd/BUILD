load("//bazel/tools/oci:go_image.bzl", "go_image")

# Firecracker snapshot/restore controller (ADR 022) container image.
# Uses a custom apko base (runtime/) that bundles dmsetup, dd, blockdev, and
# ca-certificates so the controller can manage device-mapper thin-pools at
# runtime without an extra privileged initContainer.
go_image(
    name = "image",
    base = "//projects/agent_platform/fc-agentd/runtime:base",
    binary = "//projects/agent_platform/fc-agentd/cmd",
)
