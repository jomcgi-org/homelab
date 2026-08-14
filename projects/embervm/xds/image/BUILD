# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

# embervm-xds sidecar container image: the go-control-plane ADS server that feeds
# the per-node serving Envoys (R3, PR-3). amd64 only: the Go binary is pure Go
# and could cross-compile, but nothing aarch64 consumes it. Layered at
# /usr/local/bin/embervm-xds (the apko entrypoint). Non-root uid 65532 (unlike the
# noded image, which is uid 0 for Firecracker): this is a plain network server.

platform_transition_filegroup(
    name = "xds_amd64_bin",
    srcs = ["//projects/embervm/xds/cmd:xds"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "xds_tar_amd64",
    srcs = [":xds_amd64_bin"],
    mtree = [
        "./usr/local/bin/embervm-xds type=file content=$(execpath :xds_amd64_bin) mode=0755 uid=0 gid=0",
    ],
)

apko_image(
    name = "image",
    # amd64 only: see the `arm64` arg in apko_image.bzl.
    arm64 = False,
    config = "apko.yaml",
    contents = "@embervm_xds_lock//:contents",
    # :xds_tar is the Go binary (multiarch_tars appends _amd64/_arm64 per arch).
    repository = "ghcr.io/jomcgi/homelab/projects/embervm/xds/image",
    tars = [
        ":xds_tar_amd64",
    ],
)
