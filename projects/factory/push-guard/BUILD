load("//bazel/tools/pytest:defs.bzl", "py_test")

py_test(
    name = "guard_test",
    srcs = ["guard_test.py"],
    data = ["pre-receive"],
    deps = ["@pip//pytest"],
)
