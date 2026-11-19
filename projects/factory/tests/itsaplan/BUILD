load("@bazel_skylib//:bzl_library.bzl", "bzl_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

exports_files(["fixture.bzl"])

filegroup(
    name = "repo_docs",
    srcs = ["README.md"],
    visibility = ["//projects/monolith:__pkg__"],
)

py_test(
    name = "integration_test",
    size = "large",
    srcs = ["integration_test.py"],
    data = [
        "deferred.test.ts",
        "delivery-outcome-assertions.sql",
        "delivery-outcome-backfill.sql",
        "token-usage.test.ts",
        "//projects/factory/chart:deferred_adapter",
        "//projects/factory/connector/itsaplan:delivered_usage_report",
        "//projects/factory/connector/migrations:sql",
        "//projects/platform-factory/kagent-postgres/resources:claims_test.sql",
        "//projects/platform-factory/kagent-postgres/resources:claims_test_files",
        "@itsaplan_test//:runtime/bin/bun",
        "@itsaplan_test//:upstream.tar.gz",
        "@postgres_test//:postgres",
    ],
    env = {
        "CLAIMS_TEST": "$(rootpath //projects/platform-factory/kagent-postgres/resources:claims_test.sql)",
        "BUN_BIN": "$(rootpath @itsaplan_test//:runtime/bin/bun)",
        "ITSAPLAN_ARCHIVE": "$(rootpath @itsaplan_test//:upstream.tar.gz)",
    },
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    deps = ["@pip//pytest"],
)

bzl_library(
    name = "fixture",
    srcs = ["fixture.bzl"],
    visibility = ["//visibility:public"],
)
