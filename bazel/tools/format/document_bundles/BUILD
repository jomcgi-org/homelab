# gazelle:ignore
load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_library(
    name = "check_bundles_lib",
    srcs = ["check_bundles.py"],
    imports = ["."],
)

py_test(
    name = "check_bundles_test",
    srcs = ["check_bundles_test.py"],
    data = [
        "//:docs/templates/ARCHITECTURE.md",
        "//:docs/templates/bundles.yaml",
        "//bazel/tools/ci",
    ],
    deps = [
        ":check_bundles_lib",
        "@pip//pytest",
    ],
)
