load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# The package entrypoint calls main at import time and is exercised through
# probe.cli instead of being a source of the library or unit test.
# gazelle:exclude __main__.py

semgrep_test(
    name = "__init___semgrep_test",
    srcs = ["__init__.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "cli_semgrep_test",
    srcs = ["cli.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "fixture_semgrep_test",
    srcs = ["fixture.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "probe_test_semgrep_test",
    srcs = ["probe_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "report_semgrep_test",
    srcs = ["report.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "session_semgrep_test",
    srcs = ["session.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "spans_semgrep_test",
    srcs = ["spans.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

py_library(
    name = "probe",
    srcs = [
        "__init__.py",
        "cli.py",
        "fixture.py",
        "report.py",
        "session.py",
        "spans.py",
    ],
    imports = [".."],
    visibility = ["//projects/model-bench:__subpackages__"],
    deps = [
        "//projects/model-bench/bench",
        "//projects/model-bench/bench/verifiers",
        "@pip//httpx",
    ],
)

py_test(
    name = "probe_test",
    srcs = ["probe_test.py"],
    imports = [".."],
    deps = [
        ":probe",
        "@pip//httpx",
    ],
)
