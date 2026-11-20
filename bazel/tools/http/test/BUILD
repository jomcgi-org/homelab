load("@bazel_skylib//:bzl_library.bzl", "bzl_library")
load("@rules_shell//shell:sh_test.bzl", "sh_test")

exports_files(["fixture.tar.bz2"])

sh_test(
    name = "archive_mode_test",
    srcs = ["archive_mode_test.sh"],
    args = [
        "$(rootpath @http_archive_fixture//:binary)",
        "$(rootpath @http_archive_fixture//:tar)",
    ],
    data = [
        "@http_archive_fixture//:binary",
        "@http_archive_fixture//:tar",
    ],
)

bzl_library(
    name = "fixture",
    srcs = ["fixture.bzl"],
    visibility = ["//visibility:public"],
    deps = ["//bazel/tools/http:multiarch_http_file"],
)
