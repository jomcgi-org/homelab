load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_library(
    name = "duckdb_query",
    srcs = glob(
        ["*.py"],
        exclude = ["*_test.py"],
    ),
    visibility = ["//:__subpackages__"],
    deps = ["@pip//duckdb"],
)

py_test(
    name = "query_test",
    srcs = ["query_test.py"],
    deps = [
        ":duckdb_query",
        "@pip//duckdb",
        "@pip//pytest",
    ],
)
