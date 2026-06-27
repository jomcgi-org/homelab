load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# Tools image for the base-rootfs initContainer (ADR 022): crane + e2fsprogs to
# build the agent base rootfs in-cluster from the pinned harness image, removing
# the manual node-4 sudo step. Pinned into the fc-agentd chart values.
apko_image(
    name = "image",
    config = "apko.yaml",
    contents = "@rootfs_builder_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/agent_platform/fc-agentd/rootfs-builder",
)
