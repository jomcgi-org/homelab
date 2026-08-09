load("@bazel_skylib//:bzl_library.bzl", "bzl_library")

# Export push template for apko_push rule
exports_files(
    [
        "apko_push.sh.tpl",
        "verify-apko-lock.sh",
        "verify-py3-image.sh",
    ],
    visibility = ["//visibility:public"],
)

bzl_library(
    name = "go_image",
    srcs = ["go_image.bzl"],
    visibility = ["//visibility:public"],
    deps = [
        ":providers",
        "@aspect_bazel_lib//lib:tar",
        "@aspect_bazel_lib//lib:transitions",
        "@rules_oci//oci:defs",
    ],
)

bzl_library(
    name = "py3_image",
    srcs = ["py3_image.bzl"],
    visibility = ["//visibility:public"],
    deps = [
        ":providers",
        "@aspect_bazel_lib//lib:expand_template",
        "@aspect_bazel_lib//lib:tar",
        "@aspect_bazel_lib//lib:transitions",
        "@aspect_rules_py//py:defs",
        "@rules_oci//oci:defs",
        "@rules_shell//shell:rules_bzl",
    ],
)

bzl_library(
    name = "apko_image",
    srcs = ["apko_image.bzl"],
    visibility = ["//visibility:public"],
    deps = [
        ":apko_push",
        ":oci_run",
        ":providers",
        "@aspect_bazel_lib//lib:expand_template",
        "@aspect_bazel_lib//lib:transitions",
        "@rules_apko//apko:defs",
        "@rules_oci//oci:defs",
        "@rules_shell//shell:rules_bzl",
    ],
)

bzl_library(
    name = "apko_nginx_frontend",
    srcs = ["apko_nginx_frontend.bzl"],
    visibility = ["//visibility:public"],
    deps = [":apko_image"],
)

bzl_library(
    name = "apko_push",
    srcs = ["apko_push.bzl"],
    visibility = ["//visibility:public"],
    deps = ["@bazel_skylib//lib:paths"],
)

bzl_library(
    name = "oci_run",
    srcs = ["oci_run.bzl"],
    visibility = ["//visibility:public"],
)

bzl_library(
    name = "providers",
    srcs = ["providers.bzl"],
    visibility = ["//visibility:public"],
)
