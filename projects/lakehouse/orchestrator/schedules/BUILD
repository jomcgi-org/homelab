load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# Temporal schedule definitions (ADR agents/015 §"Scheduling unified with
# execution"): one file per schedule exporting `SCHEDULES`, aggregated by the
# pkgutil loader in __init__.py. ci-format-bot (gazelle) normalizes this file.
#
# The `temporalio` wheel is in the pip lock (added by LIB-SCAFFOLD) but the
# generated gazelle module manifest (bazel/tools/python/gazelle_python.yaml,
# GENERATED / DO NOT EDIT, content-checked) was not regenerated to include it,
# so gazelle can't auto-resolve `import temporalio`. Resolve it explicitly here
# (the documented escape hatch, also used by the sibling orchestrator package).
# gazelle:resolve py temporalio @pip//temporalio
# gazelle:resolve py temporalio.client @pip//temporalio

py_library(
    name = "schedules",
    srcs = [
        "__init__.py",
        "build_serving.py",
        "gap_drain_sweep.py",
        "iceberg_batch.py",
        "tag_rotation.py",
    ],
    visibility = ["//:__subpackages__"],
    deps = [
        "//projects/lakehouse/orchestrator",
        "@pip//temporalio",
    ],
)

py_test(
    name = "schedules_test",
    srcs = ["schedules_test.py"],
    deps = [
        ":schedules",
        "//projects/lakehouse/orchestrator",
        "@pip//pytest",
        "@pip//temporalio",
    ],
)

semgrep_test(
    name = "build_serving_semgrep_test",
    srcs = ["build_serving.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "gap_drain_sweep_semgrep_test",
    srcs = ["gap_drain_sweep.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "iceberg_batch_semgrep_test",
    srcs = ["iceberg_batch.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "tag_rotation_semgrep_test",
    srcs = ["tag_rotation.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "schedules_test_semgrep_test",
    srcs = ["schedules_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "__init___semgrep_test",
    srcs = ["__init__.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)
