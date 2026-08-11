# Hand-managed targets (same convention as bazel/tools/format/readme_structure):
# the checker is invoked as plain python3 from the CI "Format check" step against
# the full working tree (a sandboxed bazel test cannot run `git ls-files` over the
# real checkout on RBE), so only the pure-logic contract is pinned by a bazel test
# here.
# gazelle:exclude check_doc_links.py
# gazelle:exclude check_doc_links_test.py

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_library(
    name = "check_doc_links_lib",
    srcs = ["check_doc_links.py"],
    imports = ["."],
)

py_test(
    name = "check_doc_links_test",
    srcs = ["check_doc_links_test.py"],
    deps = [
        ":check_doc_links_lib",
        "@pip//pytest",
    ],
)
