# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("@bazel_skylib//rules:build_test.bzl", "build_test")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

platform_transition_filegroup(
    name = "engine_gate_amd64_bin",
    srcs = ["//projects/platform-factory/substrate/egress-scan/gate"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "engine_gate_tar_amd64",
    srcs = [":engine_gate_amd64_bin"],
    mtree = ["./usr/local/bin/engine-gate type=file content=$(execpath :engine_gate_amd64_bin) mode=0755 uid=0 gid=0"],
)

apko_image(
    name = "image",
    # amd64 only: see the `arm64` arg in apko_image.bzl.
    arm64 = False,
    config = "apko.yaml",
    contents = "@egress_scan_engine_gate_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/platform-factory/substrate/egress-scan/gate",
    tars = [
        ":engine_gate_tar_amd64",
    ],
)

build_test(
    name = "image_build_test",
    targets = [":image"],
)

# The pushed image's digest, which resources/ pins (../manifests/kustomization.yaml)
# and resources_test checks.
filegroup(
    name = "image_digest",
    srcs = [":image.digest"],
    visibility = ["//projects/platform-factory/substrate/resources:__pkg__"],
)
