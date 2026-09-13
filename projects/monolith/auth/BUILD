# Bazel-native package ownership. Classification is declared here, not in
# projects/monolith/BUILD.
# Every Python test in this package has an explicit target below. Keep
# Gazelle from generating duplicate per-file py_test rules.
# gazelle:exclude *_test.py
# gazelle:exclude test_*.py

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

package(default_visibility = ["//visibility:private"])

exports_files(
    glob(["**/*.py"]),
    visibility = ["//projects/monolith:private_backend_consumers"],
)

py_library(
    name = "auth",
    srcs = glob(
        [
            "**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
        ],
    ),
    imports = [".."],
    tags = ["monolith-backend"],
    visibility = ["//projects/monolith:private_backend_consumers"],
    deps = [
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//opentelemetry_sdk",
        # PyJWT is currently locked transitively through mcp and semgrep.
        "@pip//pyjwt",
        "@pip//starlette",
    ],
)

py_test(
    name = "auth_verifier_test",
    srcs = ["verifier_test.py"],
    imports = [".."],
    deps = [
        ":auth",
        "@pip//cryptography",
        # PyJWT is currently locked transitively through mcp and semgrep.
        "@pip//pyjwt",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "auth_plumbing_test",
    srcs = ["plumbing_test.py"],
    imports = [".."],
    deps = [
        ":auth",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

# keep
py_library(
    name = "agents",
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
    tags = ["monolith-agents"],
    visibility = ["//projects/monolith:__pkg__"],
)

filegroup(
    name = "production_sources",
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
    name = "non_test_python_sources",
    srcs = glob(
        ["**/*.py"],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "**/conftest.py",
        ],
    ),
    visibility = ["//projects/monolith:__pkg__"],
)
