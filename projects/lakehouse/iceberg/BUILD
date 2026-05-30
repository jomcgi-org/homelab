load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# PyIceberg writer helpers + per-domain table schemas (ADR platform/004).
# Best-effort BUILD; ci-format-bot normalizes via gazelle and adds the
# *_semgrep_test targets.
py_library(
    name = "iceberg",
    srcs = glob(
        ["**/*.py"],
        exclude = ["**/*_test.py"],
    ),
    visibility = ["//:__subpackages__"],
    deps = [
        "@pip//pyarrow",
        "@pip//pyiceberg",
    ],
)

py_test(
    name = "tables_test",
    srcs = ["tables_test.py"],
    deps = [
        ":iceberg",
        "@pip//pyiceberg",
        "@pip//pytest",
    ],
)

py_test(
    name = "catalog_test",
    srcs = ["catalog_test.py"],
    deps = [
        ":iceberg",
        "@pip//pytest",
    ],
)

py_test(
    name = "writer_test",
    srcs = ["writer_test.py"],
    deps = [
        ":iceberg",
        "@pip//pyarrow",
        "@pip//pytest",
    ],
)

semgrep_test(
    name = "tables_test_semgrep_test",
    srcs = ["tables_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "catalog_test_semgrep_test",
    srcs = ["catalog_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "writer_test_semgrep_test",
    srcs = ["writer_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)
