# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

platform_transition_filegroup(
    name = "rootfs_store_amd64_bin",
    srcs = ["//projects/embervm/noded/cmd/rootfs-store"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "rootfs_store_tar_amd64",
    srcs = [":rootfs_store_amd64_bin"],
    mtree = ["./usr/local/bin/rootfs-store type=file content=$(execpath :rootfs_store_amd64_bin) mode=0755 uid=0 gid=0"],
)

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
    tars = [":rootfs_store_tar_amd64"],
)
