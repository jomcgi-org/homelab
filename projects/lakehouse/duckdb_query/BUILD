load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# The `duckdb` import name maps to the @pip//duckdb wheel. It is not yet in
# bazel/tools/python/gazelle_python.yaml (the LIB-SCAFFOLD unit added duckdb to
# the requirements lock but did not regenerate the manifest), so resolve it
# locally here to keep this unit independent of the shared manifest.
# gazelle:resolve py duckdb @pip//duckdb

py_library(
    name = "duckdb_query",
    srcs = [
        "__init__.py",
        "query.py",
    ],
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
