load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# Temporal orchestration substrate (ADR agents/015): client + worker skeleton.
# Real workflows/schedules and the worker image (py_venv_binary entrypoint
# `python -m projects.lakehouse.orchestrator.worker`) land in Wavefront 3.
# ci-format-bot (gazelle) normalizes this file and adds semgrep_test targets.
py_library(
    name = "orchestrator",
    srcs = glob(
        ["*.py"],
        exclude = ["*_test.py"],
    ),
    visibility = ["//:__subpackages__"],
    deps = ["@pip//temporalio"],
)

py_test(
    name = "client_test",
    srcs = ["client_test.py"],
    deps = [
        ":orchestrator",
        "@pip//pytest",
    ],
)

py_test(
    name = "worker_test",
    srcs = ["worker_test.py"],
    deps = [
        ":orchestrator",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//temporalio",
    ],
)
