load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# Force the intra-package imports to resolve to this package's own library
# rather than the parent //projects/lakehouse (which carries only the top-level
# __init__.py and not the nats_client submodules). Without this, gazelle maps
# `projects.lakehouse.nats_client.client` to //projects/lakehouse and the test
# fails with ModuleNotFoundError for the client submodule.
# gazelle:resolve py projects.lakehouse.nats_client.client //projects/lakehouse/nats_client:nats_client
# gazelle:resolve py projects.lakehouse.nats_client //projects/lakehouse/nats_client:nats_client

py_library(
    name = "nats_client",
    srcs = glob(
        ["**/*.py"],
        exclude = ["**/*_test.py"],
    ),
    visibility = ["//:__subpackages__"],
    deps = ["@pip//nats_py"],
)

py_test(
    name = "client_test",
    srcs = ["client_test.py"],
    deps = [
        ":nats_client",
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
