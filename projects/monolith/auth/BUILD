# gazelle:ignore

load("@aspect_rules_py//py:defs.bzl", "py_library")

exports_files(
    [
        "verifier_test.py",
        "plumbing_test.py",
        "platform/service_test.py",
        "platform/grimoire_test.py",
        "platform/postgres_test.py",
    ],
    visibility = ["//projects/monolith:__pkg__"],
)

filegroup(
    name = "repo_docs",
    srcs = glob(["**/*.md"]),
    visibility = ["//projects/monolith:__pkg__"],
)

filegroup(
    name = "boundary_sources",
    srcs = glob(
        ["**/*.py"],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
        ],
    ),
    visibility = ["//projects/monolith:__pkg__"],
)

# Tier-independent identity verification. No platform persistence or routers.
py_library(
    name = "auth",
    srcs = glob(
        ["*.py"],
        exclude = [
            "*_test.py",
            "module.py",
        ],
    ),
    imports = [".."],
    visibility = ["//projects/monolith:__subpackages__"],
    deps = [
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//opentelemetry_sdk",
        "@pip//pyjwt",
        "@pip//starlette",
    ],
)

# Private module only. Public and agents binaries depend on :auth instead.
py_library(
    name = "platform_auth",
    srcs = ["module.py"] + glob(
        ["platform/**/*.py"],
        exclude = ["**/*_test.py"],
    ),
    imports = [".."],
    visibility = ["//projects/monolith:__pkg__"],
    deps = [
        ":auth",
        "//projects/monolith:pkg_core",
        "//projects/monolith:pkg_framework",
        "@pip//fastapi",
        "@pip//fastmcp",
        "@pip//pydantic",
        "@pip//pyjwt",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
        "@pip//starlette",
    ],
)
