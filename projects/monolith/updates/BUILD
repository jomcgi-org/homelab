# gazelle:ignore

load("@aspect_rules_py//py:defs.bzl", "py_library")

package(default_visibility = ["//visibility:private"])

# Root compatibility tests still own their hand-written rule metadata. Export
# the files, but keep source membership and tier classification in this package.
exports_files(
    ["BUILD"] + glob(
        ["**/*"],
        exclude = ["BUILD"],
    ),
    visibility = ["//projects/monolith:__pkg__"],
)

filegroup(
    name = "runtime_sources",
    srcs = glob(
        ["**/*.py"],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "tests/**/*.py",
        ],
    ),
    visibility = ["//projects/monolith:__pkg__"],
)

# Source inventories used by root-level static tests. These deliberately differ
# from runtime_sources: boundary scans retain offline modules, while the broad
# source scan retains non-test helpers under tests/ but excludes conftest.py.
filegroup(
    name = "boundary_sources",
    srcs = glob(
        ["**/*.py"],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "tests/**/*.py",
        ],
    ),
    visibility = ["//projects/monolith:__pkg__"],
)

filegroup(
    name = "source_scan_files",
    srcs = glob(
        ["**/*.py"],
        exclude = [
            "**/*_test.py",
            "**/conftest.py",
            "**/test_*.py",
        ],
    ),
    visibility = ["//projects/monolith:__pkg__"],
)

filegroup(
    name = "bdd_tests",
    srcs = glob(
        ["tests/*_test.py"],
        allow_empty = True,
    ),
    visibility = ["//projects/monolith:__pkg__"],
)

py_library(
    name = "pkg_updates",
    srcs = glob(
        [
            "**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "tests/**/*.py",
        ],
    ),
    imports = [".."],
    tags = ["monolith-private"],
    visibility = ["//projects/monolith:package_consumers"],
    deps = [
        "//projects/monolith/auth:pkg_auth",
        "//projects/monolith/core:pkg_core",
        "//projects/monolith/framework:pkg_framework",
        "@pip//fastapi",
        "@pip//fastmcp",
        "@pip//pydantic",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)
