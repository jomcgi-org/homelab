# gazelle:ignore
load("@aspect_rules_py//py:defs.bzl", "py_binary", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_binary(
    name = "check_bundles",
    srcs = ["check_bundles.py"],
    deps = ["@pip//pyyaml"],
)

py_library(
    name = "check_bundles_lib",
    srcs = ["check_bundles.py"],
    imports = ["."],
    deps = ["@pip//pyyaml"],
)

py_test(
    name = "check_bundles_test",
    srcs = ["check_bundles_test.py"],
    data = ["//:docs/templates/bundles.yaml"],
    deps = [
        ":check_bundles_lib",
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)
