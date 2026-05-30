# HAND-WRITTEN, gazelle-excluded (see `# gazelle:exclude workflows` in
# projects/lakehouse/orchestrator/BUILD). This package is the conflict-free seam
# for Wavefront-3 workflow units: each drops ONE `<workflow_id>.py` file here
# (exporting module-level WORKFLOWS / ACTIVITIES) plus a `<workflow_id>_test.py`,
# and touches NO BUILD file — the globs below pick them up automatically and the
# loader (__init__.py) + run.py discover them at runtime. `deps` is a superset
# covering every lakehouse workflow so a new file needs no dep edit here.
load("@aspect_rules_py//py:defs.bzl", "py_library")
load("@aspect_rules_py//py/private/py_venv:defs.bzl", "py_venv_binary")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

_WORKFLOW_DEPS = [
    "//projects/lakehouse/events",
    "//projects/lakehouse/nats_client",
    "//projects/lakehouse/iceberg",
    "//projects/lakehouse/duckdb_query",
    "//projects/lakehouse/orchestrator",
    "@pip//temporalio",
    "@pip//pyiceberg",
    "@pip//pyarrow",
    "@pip//duckdb",
    "@pip//psycopg",
    "@pip//psycopg_binary",
    "@pip//pydantic",
]

py_library(
    name = "workflows",
    srcs = glob(["**/*.py"], exclude = ["**/*_test.py"]),
    visibility = ["//:__subpackages__"],
    deps = _WORKFLOW_DEPS,
)

# Worker entrypoint binary: `python -m projects.lakehouse.orchestrator.workflows.run`.
# The Wavefront-3 worker apko image wraps this; many Deployments share it,
# differentiated only by the TASK_QUEUE env var.
py_venv_binary(
    name = "worker_main",
    srcs = glob(["**/*.py"], exclude = ["**/*_test.py"]),
    main = "run.py",
    visibility = ["//:__subpackages__"],
    deps = _WORKFLOW_DEPS,
)

# One glob'd test target (not one-per-file) so adding a workflow test requires
# no edit here — keeping parallel workflow units conflict-free.
py_test(
    name = "workflows_test",
    srcs = glob(["**/*_test.py"]),
    deps = [
        ":workflows",
        "@pip//pytest",
    ] + _WORKFLOW_DEPS,
)

semgrep_test(
    name = "workflows_semgrep_test",
    srcs = glob(["**/*.py"]),
    rules = ["//bazel/semgrep/rules:python_rules"],
)
