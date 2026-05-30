load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")

# Top-level package marker for the event-sourced lakehouse project.
# Sub-packages (events/, nats_client/, orchestrator/, iceberg/, duckdb_query/,
# dispatchers/) carry their own BUILD files and are added by their respective
# Wavefront-2/3/4 units. Worker + quack-server image targets are added here by
# the Wavefront-3 image units.
py_library(
    name = "lakehouse",
    srcs = ["__init__.py"],
    visibility = ["//:__subpackages__"],
)

semgrep_test(
    name = "__init___semgrep_test",
    srcs = ["__init__.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)
