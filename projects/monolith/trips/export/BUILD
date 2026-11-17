# gazelle:ignore
load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# Package boundary keeps the private exporter out of the public backend glob.
py_library(
    name = "export",
    srcs = ["__init__.py"],
    imports = ["../.."],
    visibility = ["//projects/monolith:__pkg__"],
    deps = [
        "//projects/monolith:pkg_core",
        "//projects/monolith:pkg_trips",
        "@pip//boto3",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "export_test",
    srcs = ["export_test.py"],
    imports = ["../.."],
    deps = [
        ":export",
        "@pip//boto3",
        "@pip//pytest",
    ],
)
