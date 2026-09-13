# gazelle:ignore

load("@aspect_rules_py//py:defs.bzl", "py_library")

package(default_visibility = ["//visibility:private"])

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
    name = "pkg_auth",
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
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//opentelemetry_sdk",
        # PyJWT is currently locked transitively through mcp and semgrep.
        "@pip//pyjwt",
        "@pip//starlette",
    ],
)

py_library(
    name = "agents",
    srcs = [":runtime_sources"],
    imports = [".."],
    tags = ["monolith-agents"],
    visibility = ["//projects/monolith:__pkg__"],
)
