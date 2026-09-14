# gazelle:ignore
load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_library(
    name = "run_linux_lib",
    srcs = ["run_linux.py"],
    imports = ["."],
)

py_test(
    name = "run_linux_test",
    srcs = ["run_linux_test.py"],
    deps = [
        ":run_linux_lib",
        "@pip//pytest",
    ],
)
