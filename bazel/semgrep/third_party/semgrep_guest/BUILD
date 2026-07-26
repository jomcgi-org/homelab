load("@bazel_skylib//:bzl_library.bzl", "bzl_library")

bzl_library(
    name = "extensions",
    srcs = ["extensions.bzl"],
    visibility = ["//visibility:public"],
    deps = [
        ":digests",
        "//bazel/semgrep/third_party/semgrep_pro:oci_archive",
    ],
)

bzl_library(
    name = "digests",
    srcs = ["digests.bzl"],
    visibility = ["//visibility:public"],
)
