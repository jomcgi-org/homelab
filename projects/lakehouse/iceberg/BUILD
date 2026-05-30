load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# PyIceberg writer helpers + per-domain table schemas (ADR platform/004).
#
# pyiceberg/pyarrow are present in the requirements lock (added by LIB-SCAFFOLD)
# but were not yet added to the gazelle python manifest
# (bazel/tools/python/gazelle_python.yaml), so gazelle's resolver can't map the
# `import pyiceberg` / `import pyarrow` statements to their @pip targets. These
# directives resolve them explicitly without editing the shared manifest; they
# are inherited by the gazelle-generated tables/ sub-package BUILD.
# gazelle:resolve py pyiceberg @pip//pyiceberg
# gazelle:resolve py pyiceberg.schema @pip//pyiceberg
# gazelle:resolve py pyiceberg.types @pip//pyiceberg
# gazelle:resolve py pyiceberg.catalog @pip//pyiceberg
# gazelle:resolve py pyiceberg.io.pyarrow @pip//pyiceberg
# gazelle:resolve py pyiceberg.table @pip//pyiceberg
# gazelle:resolve py pyarrow @pip//pyarrow

py_library(
    name = "iceberg",
    srcs = [
        "__init__.py",
        "catalog.py",
        "writer.py",
    ],
    visibility = ["//:__subpackages__"],
    deps = [
        "@pip//pyarrow",
        "@pip//pyiceberg",
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
        "//projects/lakehouse/iceberg/tables",
        "@pip//pyarrow",
        "@pip//pytest",
    ],
)

py_test(
    name = "tables_test",
    srcs = ["tables_test.py"],
    deps = [
        "//projects/lakehouse/iceberg/tables",
        "@pip//pyiceberg",
        "@pip//pytest",
    ],
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

semgrep_test(
    name = "tables_test_semgrep_test",
    srcs = ["tables_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "__init___semgrep_test",
    srcs = ["__init__.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "catalog_semgrep_test",
    srcs = ["catalog.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "writer_semgrep_test",
    srcs = ["writer.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)
