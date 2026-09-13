# Bazel-native package ownership. Classification is declared here, not in
# projects/monolith/BUILD.
# The root monolith Semgrep target already scans this package transitively.
# gazelle:semgrep disabled
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

# keep: recursive sources and reviewed runtime dependencies are package-owned.
py_library(
    name = "core",
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
        "@pip//certifi",
        "@pip//fastapi",
        "@pip//fastmcp",
        "@pip//httpx",
        "@pip//opentelemetry_exporter_otlp_proto_http",
        "@pip//opentelemetry_instrumentation_fastapi",
        "@pip//opentelemetry_instrumentation_httpx",
        "@pip//opentelemetry_sdk",
        "@pip//pgvector",
        "@pip//psycopg",
        "@pip//psycopg_binary",
        "@pip//pydantic",
        "@pip//python_multipart",
        "@pip//pyyaml",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
        "@pip//tzdata",
        "@pip//uvicorn",
    ],
)

py_test(
    name = "db_test",
    srcs = ["db_test.py"],
    imports = [".."],
    deps = [
        ":core",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "github_test",
    srcs = ["github_test.py"],
    imports = [".."],
    deps = [
        ":core",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "mcp_policy_test",
    srcs = ["mcp_policy_test.py"],
    imports = [".."],
    deps = [
        ":core",
        "//projects/monolith/auth",
        "@pip//fastmcp",
        "@pip//pytest",
    ],
)

py_test(
    name = "leadership_test",
    srcs = ["leadership_test.py"],
    imports = [".."],
    deps = [
        ":core",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "log_test",
    srcs = ["log_test.py"],
    imports = [".."],
    deps = [
        ":core",
        "@pip//pytest",
    ],
)

py_test(
    name = "log_config_test",
    srcs = ["log_config_test.py"],
    imports = [".."],
    deps = [
        ":core",
        "@pip//pytest",
    ],
)

py_test(
    name = "core_platform_probe_test",
    srcs = ["platform_probe_test.py"],
    imports = [".."],
    deps = [
        ":core",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

# keep
py_library(
    name = "public",
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
    tags = ["monolith-public"],
    visibility = ["//projects/monolith/public_backend:__pkg__"],
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
            "mcp_app.py",
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
