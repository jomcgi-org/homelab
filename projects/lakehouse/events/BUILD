load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_library(
    name = "events",
    srcs = [
        "__init__.py",
        "envelope.py",
        "publish.py",
        "versioning.py",
    ],
    visibility = ["//:__subpackages__"],
    deps = ["@pip//pydantic"],
)

py_test(
    name = "envelope_test",
    srcs = ["envelope_test.py"],
    deps = [
        ":events",
        "@pip//pydantic",
        "@pip//pytest",
    ],
)

py_test(
    name = "publish_test",
    srcs = ["publish_test.py"],
    deps = [
        ":events",
        "@pip//pytest",
    ],
)

py_test(
    name = "versioning_test",
    srcs = ["versioning_test.py"],
    deps = [
        ":events",
        "@pip//pytest",
    ],
)

semgrep_test(
    name = "envelope_semgrep_test",
    srcs = ["envelope.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "publish_semgrep_test",
    srcs = ["publish.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "versioning_semgrep_test",
    srcs = ["versioning.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "__init___semgrep_test",
    srcs = ["__init__.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)
