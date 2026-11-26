# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

platform_transition_filegroup(
    name = "connector_amd64_bin",
    srcs = ["//projects/scotland-connector/cmd/connector"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "connector_tar_amd64",
    srcs = [":connector_amd64_bin"],
    mtree = ["./usr/local/bin/connector type=file content=$(execpath :connector_amd64_bin) mode=0755 uid=0 gid=0"],
)

# Same package set as the tokenbroker image (CA bundle for R2's TLS, busybox
# for debugging), so the lock was seeded from it with only config.checksum
# recomputed. A package change here needs a full re-resolve.
apko_image(
    name = "image",
    # amd64 only: see the `arm64` arg in apko_image.bzl.
    arm64 = False,
    config = "apko.yaml",
    contents = "@scotland_connector_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/scotland-connector",
    tars = [
        ":connector_tar_amd64",
    ],
)
