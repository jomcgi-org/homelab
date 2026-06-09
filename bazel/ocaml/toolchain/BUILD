load("@bazel_skylib//:bzl_library.bzl", "bzl_library")

bzl_library(
    name = "repositories",
    srcs = ["repositories.bzl"],
    visibility = ["//visibility:public"],
    deps = [":source"],
)

bzl_library(
    name = "source",
    srcs = ["source.bzl"],
    visibility = ["//visibility:public"],
)
