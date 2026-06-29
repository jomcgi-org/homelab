load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# Runtime base image for fc-agentd: provides dmsetup (lvm2), dd + blockdev
# (busybox/util-linux), and ca-certificates so the controller can manage
# device-mapper thin-pools without an extra privileged initContainer.
apko_image(
    name = "base",
    config = "apko.yaml",
    contents = "@fc_agentd_runtime_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/agent_platform/fc-agentd/runtime-base",
    visibility = [
        "//bazel/images:__pkg__",
        "//projects/agent_platform/fc-agentd:__subpackages__",
    ],
)
