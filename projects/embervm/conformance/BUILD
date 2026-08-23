# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("@rules_go//go:def.bzl", "go_binary", "go_library", "go_test")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

go_library(
    name = "conformance_lib",
    srcs = [
        "main.go",
        "scenarios.go",
        "verdict.go",
    ],
    importpath = "github.com/jomcgi/homelab/projects/embervm/conformance",
    visibility = ["//visibility:private"],
)

go_binary(
    name = "conformance",
    embed = [":conformance_lib"],
    visibility = ["//visibility:private"],
)

go_test(
    name = "conformance_test",
    srcs = [
        "scenarios_test.go",
        "verdict_test.go",
    ],
    embed = [":conformance_lib"],
)

platform_transition_filegroup(
    name = "conformance_amd64_bin",
    srcs = [":conformance"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "conformance_tar_amd64",
    srcs = [":conformance_amd64_bin"],
    mtree = ["./usr/local/bin/conformance type=file content=$(execpath :conformance_amd64_bin) mode=0755 uid=0 gid=0"],
)

apko_image(
    name = "image",
    # amd64 only: see the `arm64` arg in apko_image.bzl.
    arm64 = False,
    config = "apko.yaml",
    contents = "@embervm_conformance_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/embervm/conformance",
    tars = [":conformance_tar_amd64"],
)
