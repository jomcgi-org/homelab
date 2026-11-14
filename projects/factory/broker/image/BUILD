# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

platform_transition_filegroup(
    name = "broker_amd64_bin",
    srcs = ["//projects/factory/broker/cmd/broker"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "broker_tar_amd64",
    srcs = [":broker_amd64_bin"],
    mtree = ["./usr/local/bin/broker type=file content=$(execpath :broker_amd64_bin) mode=0755 uid=0 gid=0"],
)

apko_image(
    name = "image",
    # amd64 only: see the `arm64` arg in apko_image.bzl.
    arm64 = False,
    config = "apko.yaml",
    contents = "@factory_broker_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/factory/broker",
    tars = [
        ":broker_tar_amd64",
    ],
)
