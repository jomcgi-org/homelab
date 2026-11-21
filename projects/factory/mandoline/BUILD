load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_library(
    name = "execution",
    srcs = ["execution.py"],
    visibility = ["//:__subpackages__"],
)

py_test(
    name = "execution_test",
    srcs = ["execution_test.py"],
    deps = [
        ":execution",
        "@pip//pytest",
    ],
)
