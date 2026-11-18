# gazelle:exclude check_invariants.py
# gazelle:exclude check_invariants_test.py

load("@aspect_rules_py//py:defs.bzl", "py_binary", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_library(
    name = "check_invariants_lib",
    srcs = ["check_invariants.py"],
    imports = ["."],
    deps = ["@pip//pyyaml"],
)

py_binary(
    name = "check_invariants",
    srcs = ["check_invariants.py"],
    deps = ["@pip//pyyaml"],
)

py_test(
    name = "check_invariants_test",
    srcs = ["check_invariants_test.py"],
    deps = [
        ":check_invariants_lib",
        "@pip//pytest",
    ],
)
