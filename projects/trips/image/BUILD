# gazelle:ignore
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_bazel_lib//lib:transitions.bzl", "platform_transition_filegroup")
load("//bazel/tools/oci:apko_image.bzl", "apko_image")

platform_transition_filegroup(
    name = "ingest_amd64",
    srcs = ["//projects/trips/ingest"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

platform_transition_filegroup(
    name = "export_amd64",
    srcs = ["//projects/trips/export"],
    target_platform = "@rules_go//go/toolchain:linux_amd64",
)

tar(
    name = "commands",
    srcs = [
        ":export_amd64",
        ":ingest_amd64",
    ],
    mtree = [
        "./usr/local/bin/ingest type=file content=$(execpath :ingest_amd64) mode=0755 uid=0 gid=0",
        "./usr/local/bin/export type=file content=$(execpath :export_amd64) mode=0755 uid=0 gid=0",
    ],
)

apko_image(
    name = "image",
    arm64 = False,
    config = "apko.yaml",
    contents = "@trips_lock//:contents",
    repository = "ghcr.io/jomcgi/homelab/projects/trips",
    tars = [":commands"],
)
