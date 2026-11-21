load("//bazel/tools/pytest:defs.bzl", "py_test")

exports_files(
    ["pre-receive"],
    visibility = ["//projects/factory/push-guard:__subpackages__"],
)

filegroup(
    name = "repo_docs",
    srcs = ["README.md"],
    visibility = ["//projects/monolith:__pkg__"],
)

py_test(
    name = "guard_test",
    srcs = ["guard_test.py"],
    data = ["pre-receive"],
    deps = ["@pip//pytest"],
)
