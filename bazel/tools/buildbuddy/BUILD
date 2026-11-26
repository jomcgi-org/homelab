# Hand-managed target. The script runs as plain python3 against a live API,
# so only the pure-logic contract is pinned by a bazel test.
# gazelle:exclude bb_usage.py
# gazelle:exclude bb_usage_test.py

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_library(
    name = "bb_usage_lib",
    srcs = ["bb_usage.py"],
    imports = ["."],
)

py_test(
    name = "bb_usage_test",
    srcs = ["bb_usage_test.py"],
    deps = [
        ":bb_usage_lib",
        "@pip//pytest",
    ],
)
