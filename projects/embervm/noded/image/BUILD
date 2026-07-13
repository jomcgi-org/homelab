# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# embervm-noded daemon container image. Built as an apko image (Wolfi base) rather
# than a distroless go_image because it drives Firecracker guests as child
# processes, mirroring the fc-invoke daemon it forks. AMD64-ONLY: node-4 is the
# lone Firecracker node and is amd64, and FC snapshots are arch-affine, so no
# aarch64 variant is built (apko_image arm64 = False). The Go binary is
# cross-compiled for linux/amd64 and layered at /usr/local/bin/embervm-noded (the
# apko entrypoint); @kata_firecracker bakes firecracker + the guest kernel at
# /opt/fc so the node needs no /opt/kata install.

platform_transition_filegroup(
    name = "noded_amd64_bin",
    srcs = ["//projects/embervm/noded/cmd:noded"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "noded_tar_amd64",
    srcs = [":noded_amd64_bin"],
    mtree = [
        "./usr/local/bin/embervm-noded type=file content=$(execpath :noded_amd64_bin) mode=0755 uid=0 gid=0",
    ],
)

apko_image(
    name = "image",
    # amd64-only: node-4 is the sole FC node and is amd64; FC snapshots are
    # arch-affine, so :image is a single-platform amd64 image, not an index.
    arm64 = False,
    config = "apko.yaml",
    contents = "@embervm_noded_lock//:contents",
    # :noded_tar is the Go binary (multiarch_tars appends _amd64); @kata_firecracker
    # bakes the firecracker binary + guest kernel into /opt/fc.
    multiarch_tars = [
        ":noded_tar",
        "@kata_firecracker//:tar",
    ],
    repository = "ghcr.io/jomcgi/homelab/projects/embervm/noded/image",
)
