load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# Temporal orchestration substrate (ADR agents/015): client + worker skeleton.
# Real workflows/schedules and the worker image (py_venv_binary entrypoint
# `python -m projects.lakehouse.orchestrator.worker`) land in Wavefront 3.
# ci-format-bot (gazelle) normalizes this file and adds semgrep_test targets.
#
# The `temporalio` wheel is in the pip lock (added by LIB-SCAFFOLD) but the
# generated gazelle module manifest (bazel/tools/python/gazelle_python.yaml) was
# not regenerated to include it, so gazelle can't auto-resolve `import
# temporalio`. Until that shared manifest is regenerated (a serialized scaffold
# step — the file is GENERATED / DO NOT EDIT and carries a content check),
# resolve the imports explicitly here. These directives are local to this
# package and the documented gazelle escape hatch for this exact case.
# gazelle:resolve py temporalio @pip//temporalio
# gazelle:resolve py temporalio.client @pip//temporalio
# gazelle:resolve py temporalio.worker @pip//temporalio

py_library(
    name = "orchestrator",
    srcs = [
        "__init__.py",
        "client.py",
        "worker.py",
    ],
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
