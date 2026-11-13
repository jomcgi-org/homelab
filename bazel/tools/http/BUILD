# gazelle:exclude
load("@bazel_skylib//:bzl_library.bzl", "bzl_library")
load(":compatibility.bzl", "compatibility_test")

"""HTTP utilities for downloading and packaging binaries"""

exports_files(
    ["multiarch_http_file.bzl"],
    visibility = ["//visibility:public"],
)

bzl_library(
    name = "multiarch_http_file",
    srcs = ["multiarch_http_file.bzl"],
    visibility = ["//visibility:public"],
    deps = [
        "@bazel_tools//tools/build_defs/repo:http.bzl",
    ],
)

bzl_library(
    name = "k3s_archive",
    srcs = ["k3s_archive.bzl"],
    visibility = ["//visibility:public"],
)

bzl_library(
    name = "kata_firecracker_archive",
    srcs = ["kata_firecracker_archive.bzl"],
    visibility = ["//visibility:public"],
)

exports_files(
    glob(["fixtures/*"]),
    visibility = ["//visibility:public"],
)

compatibility_test(
    name = "amd64_compatibility_test",
    arches = ["amd64"],
    metadata = True,
    repo = "http_fixture_amd64",
)

compatibility_test(
    name = "arm64_compatibility_test",
    arches = ["arm64"],
    metadata = True,
    repo = "http_fixture_arm64",
)

compatibility_test(
    name = "dual_compatibility_test",
    arches = [
        "amd64",
        "arm64",
    ],
    metadata = True,
    repo = "http_fixture_dual",
)

compatibility_test(
    name = "raw_compatibility_test",
    arches = ["arm64"],
    archive = False,
    metadata = True,
    repo = "http_fixture_raw",
)

# Protect directory and file assets beside a non-executable archive entry.
compatibility_test(
    name = "assets_compatibility_test",
    arches = ["amd64"],
    assets = True,
    metadata = True,
    repo = "http_fixture_assets",
)

compatibility_test(
    name = "goose_compatibility_test",
    arches = [
        "amd64",
        "arm64",
    ],
    goose = True,
    metadata = True,
    repo = "goose",
)
