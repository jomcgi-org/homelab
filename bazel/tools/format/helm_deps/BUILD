# Hand-managed targets (same convention as bazel/tools/format/readme_structure):
# the checker is invoked as plain python3 from the CI "Format check" step against
# the full working tree (a sandboxed bazel test cannot list projects/* on RBE),
# so only the pure-logic contract is pinned by a bazel test here.
# gazelle:exclude check_helm_deps.py
# gazelle:exclude check_helm_deps_test.py

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_library(
    name = "check_helm_deps_lib",
    srcs = ["check_helm_deps.py"],
    imports = ["."],
)

py_test(
    name = "check_helm_deps_test",
    srcs = ["check_helm_deps_test.py"],
    deps = [
        ":check_helm_deps_lib",
        "@pip//pytest",
    ],
)
