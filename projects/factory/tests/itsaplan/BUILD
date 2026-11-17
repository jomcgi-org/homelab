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
        "//projects/factory/chart:deferred_adapter",
        "@itsaplan_test//:runtime/bin/bun",
        "@itsaplan_test//:upstream.tar.gz",
        "@postgres_test//:postgres",
    ],
    env = {
        "BUN_BIN": "$(rootpath @itsaplan_test//:runtime/bin/bun)",
        "ITSAPLAN_ARCHIVE": "$(rootpath @itsaplan_test//:upstream.tar.gz)",
    },
    target_compatible_with = [
        "@platforms//cpu:x86_64",
        "@platforms//os:linux",
    ],
    deps = ["@pip//pytest"],
)
