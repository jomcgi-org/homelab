load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

py_library(
    name = "nats_client",
    srcs = glob(
        ["**/*.py"],
        exclude = ["**/*_test.py"],
    ),
    visibility = ["//:__subpackages__"],
    deps = [
        "//projects/lakehouse",
        "@pip//nats_py",
    ],
)

py_test(
    name = "client_test",
    srcs = ["client_test.py"],
    deps = [
        "//projects/lakehouse",
        "@pip//pytest",
    ],
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
