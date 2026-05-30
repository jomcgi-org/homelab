# gazelle:semgrep_exclude_rules no-hardcoded-k8s-service-url
load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# The `duckdb` import name maps to the @pip//duckdb wheel. It is not yet in
# bazel/tools/python/gazelle_python.yaml (the LIB-SCAFFOLD unit added duckdb to
# the requirements lock but did not regenerate the manifest), so resolve it
# locally here to keep this unit independent of the shared manifest.
# gazelle:resolve py duckdb @pip//duckdb
# gazelle:resolve py certifi @pip//certifi

py_library(
    name = "duckdb_query",
    srcs = [
        "__init__.py",
        "query.py",
    ],
    visibility = ["//:__subpackages__"],
    deps = [
        "@pip//certifi",
        "@pip//duckdb",
    ],
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

semgrep_test(
    name = "__init___semgrep_test",
    srcs = ["__init__.py"],
    exclude_rules = ["no-hardcoded-k8s-service-url"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "query_semgrep_test",
    srcs = ["query.py"],
    exclude_rules = ["no-hardcoded-k8s-service-url"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "query_test_semgrep_test",
    srcs = ["query_test.py"],
    exclude_rules = ["no-hardcoded-k8s-service-url"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)
