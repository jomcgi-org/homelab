load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")

# Per-domain Iceberg table schemas, auto-discovered into the TABLES registry.
# pyiceberg import resolution comes from the # gazelle:resolve directives in the
# parent projects/lakehouse/iceberg/BUILD (inherited into this sub-package).
py_library(
    name = "tables",
    srcs = [
        "__init__.py",
        "gap_events.py",
        "note_events.py",
    ],
    visibility = ["//:__subpackages__"],
    deps = [
        "@pip//pyiceberg",
    ],
)

semgrep_test(
    name = "__init___semgrep_test",
    srcs = ["__init__.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "gap_events_semgrep_test",
    srcs = ["gap_events.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "note_events_semgrep_test",
    srcs = ["note_events.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)
