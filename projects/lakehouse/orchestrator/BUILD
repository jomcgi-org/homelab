load("@aspect_rules_py//py:defs.bzl", "py_library")
load("@aspect_rules_py//py/private/py_venv:defs.bzl", "py_venv_binary")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_target_test", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# Temporal orchestration substrate (ADR agents/015): client + worker skeleton.
# The `worker` py_venv_binary is the entrypoint
# (`python -m projects.lakehouse.orchestrator.worker`); Wavefront 3 wraps it in
# an apko image. ci-format-bot (gazelle) normalizes this file and adds
# semgrep_test targets.
#
# The `temporalio` wheel is in the pip lock (added by LIB-SCAFFOLD) but the
# generated gazelle module manifest (bazel/tools/python/gazelle_python.yaml,
# GENERATED / DO NOT EDIT, content-checked) was not regenerated to include it,
# so gazelle can't auto-resolve `import temporalio`. Until that shared manifest
# is regenerated (a serialized scaffold step), resolve the imports explicitly
# here — the documented gazelle escape hatch, local to this package.
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

py_venv_binary(
    name = "worker",
    srcs = ["worker.py"],
    main = "worker.py",
    visibility = ["//:__subpackages__"],
    deps = [
        ":orchestrator",
        "@pip//temporalio",
    ],
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
    ],
)

semgrep_target_test(
    name = "worker_semgrep_test",
    lockfiles = ["//bazel/requirements:all.txt"],
    rules = ["//bazel/semgrep/rules:python_rules"],
    sca_rules = ["//bazel/semgrep/rules:sca_python_rules"],
    target = ":worker",
)

semgrep_test(
    name = "__init___semgrep_test",
    srcs = ["__init__.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "client_semgrep_test",
    srcs = ["client.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "client_test_semgrep_test",
    srcs = ["client_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "worker_test_semgrep_test",
    srcs = ["worker_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)
