load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

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
        "@pip//nats_py",
        "@pip//pytest",
    ],
)
