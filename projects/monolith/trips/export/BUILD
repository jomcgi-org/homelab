# gazelle:ignore
load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

# Parent globs stop here, so supply source data to the domain import guard.
filegroup(
    name = "boundary_sources",
    srcs = glob(
        ["**/*.py"],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "**/tests/**/*.py",
        ],
    ),
    visibility = ["//projects/monolith:__pkg__"],
)

# Markdown read by the monolith doc-manifest genrules. See
# //projects/monolith:repo_docs_srcs.
filegroup(
    name = "repo_docs",
    srcs = ["README.md"],
    visibility = ["//projects/monolith:__pkg__"],
)

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
