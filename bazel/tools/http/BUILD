load("@bazel_skylib//:bzl_library.bzl", "bzl_library")

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
    name = "multiarch_http_archive",
    srcs = ["multiarch_http_archive.bzl"],
    visibility = ["//visibility:public"],
)

bzl_library(
    name = "kata_firecracker_archive",
    srcs = ["kata_firecracker_archive.bzl"],
    visibility = ["//visibility:public"],
)
