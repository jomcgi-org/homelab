# Hand-managed targets (same convention as projects/firecracker/tools/env_readme):
# the checker is invoked as plain python3 from the CI "Format check" step against
# the full working tree (a sandboxed bazel test cannot list projects/* on RBE),
# so only the pure-logic contract is pinned by a bazel test here.
# gazelle:exclude check_invariants.py
# gazelle:exclude check_invariants_test.py

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_library(
    name = "check_invariants_lib",
    srcs = ["check_invariants.py"],
    imports = ["."],
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
