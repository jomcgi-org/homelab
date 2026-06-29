load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# The apko_image macro applies its `visibility` arg only to the `.push`/`.info`
# targets, not to the `:base` image itself, which go_image consumes as a base
# from an internal target in the parent package. Set package default visibility
# so `:base` (and the macro's other generated sub-targets) are reachable from the
# fc-agentd package.
package(default_visibility = [
    "//bazel/images:__pkg__",
    "//projects/agent_platform/fc-agentd:__subpackages__",
])

# Runtime base image for fc-agentd: provides dmsetup (lvm2), dd + blockdev
# (busybox/util-linux), and ca-certificates so the controller can manage
# device-mapper thin-pools without an extra privileged initContainer.
apko_image(
    name = "base",
    config = "apko.yaml",
    contents = "@fc_agentd_runtime_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/agent_platform/fc-agentd/runtime-base",
)
