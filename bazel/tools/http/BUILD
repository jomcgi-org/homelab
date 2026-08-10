load("@bazel_skylib//:bzl_library.bzl", "bzl_library")
load("@rules_shell//shell:sh_test.bzl", "sh_test")

"""HTTP utilities for downloading and packaging binaries"""

exports_files(
    ["multiarch_http_file.bzl"],
    visibility = ["//visibility:public"],
)

# Fetched by label from bazel_demo_workspace.bzl's repository rule, so the repo
# re-fetches when the archiving changes.
exports_files(
    ["stage_layer_tar.sh"],
    visibility = ["//visibility:public"],
)

sh_test(
    name = "stage_layer_tar_test",
    srcs = ["stage_layer_tar_test.sh"],
    data = [":stage_layer_tar.sh"],
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
    name = "bazel_demo_workspace",
    srcs = ["bazel_demo_workspace.bzl"],
    visibility = ["//visibility:public"],
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

bzl_library(
    name = "multiarch_http_archive",
    srcs = ["multiarch_http_archive.bzl"],
    visibility = ["//visibility:public"],
)
