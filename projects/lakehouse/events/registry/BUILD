load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_library(
    name = "registry",
    srcs = [
        "__init__.py",
        "gap.py",
        "note.py",
    ],
    visibility = ["//:__subpackages__"],
    deps = ["@pip//pydantic"],
)

py_test(
    name = "registry_test",
    srcs = ["registry_test.py"],
    deps = [
        ":registry",
        "@pip//pydantic",
    ],
)

semgrep_test(
    name = "gap_semgrep_test",
    srcs = ["gap.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "note_semgrep_test",
    srcs = ["note.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "__init___semgrep_test",
    srcs = ["__init__.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "registry_test_semgrep_test",
    srcs = ["registry_test.py"],
    rules = ["//bazel/semgrep/rules:python_rules"],
)
