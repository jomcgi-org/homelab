load("@bazel_skylib//:bzl_library.bzl", "bzl_library")

bzl_library(
    name = "extensions",
    srcs = ["extensions.bzl"],
    visibility = ["//visibility:public"],
    deps = [":oci_envoy"],
)

bzl_library(
    name = "oci_envoy",
    srcs = ["oci_envoy.bzl"],
    visibility = ["//visibility:public"],
)
