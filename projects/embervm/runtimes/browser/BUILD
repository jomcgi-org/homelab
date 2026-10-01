# gazelle:ignore
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_test(
    name = "evidence_test",
    srcs = [
        "evidence.py",
        "evidence_test.py",
    ],
    imports = ["."],
    deps = ["@pip//pytest"],
)
