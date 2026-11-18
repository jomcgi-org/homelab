# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("@bazel_skylib//rules:build_test.bzl", "build_test")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

platform_transition_filegroup(
    name = "alert_relay_amd64_bin",
    srcs = ["//projects/factory/alert-relay"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "alert_relay_tar_amd64",
    srcs = [":alert_relay_amd64_bin"],
    mtree = ["./usr/local/bin/alert-relay type=file content=$(execpath :alert_relay_amd64_bin) mode=0755 uid=0 gid=0"],
)

apko_image(
    name = "image",
    # amd64 only: see the `arm64` arg in apko_image.bzl.
    arm64 = False,
    config = "apko.yaml",
    contents = "@factory_alert_relay_lock//:contents",
    # Bump with the Kustomize Deployment when releasing a changed relay.
    release_tags = ["v2"],
    repository = "ghcr.io/jomcgi/homelab/projects/factory/alert-relay",
    tars = [
        ":alert_relay_tar_amd64",
    ],
)

build_test(
    name = "image_build_test",
    targets = [":image"],
)
