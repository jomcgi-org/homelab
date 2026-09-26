load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# The package entrypoint calls main at import time and is exercised through
# probe.cli instead of being a source of the library or unit test.
# gazelle:exclude __main__.py

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
