load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# Tools image for the fc-invoke base-rootfs initContainers (ADR 030): a generic
# crane + e2fsprogs image that builds each workload's base rootfs in-cluster from
# its pinned guest image, removing the manual node-4 sudo step. Pinned into the
# fc-invoke chart values (rootfsBuilder.image).
apko_image(
    name = "image",
    # amd64 only: see the `arm64` arg in apko_image.bzl.
    arm64 = False,
    config = "apko.yaml",
    contents = "@rootfs_builder_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/firecracker/substrate/rootfs-builder",
)
