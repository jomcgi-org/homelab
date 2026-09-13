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
    name = "cluster",
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
        "//projects/monolith/core",
        "//projects/monolith/framework",
        "@pip//certifi",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//kubernetes_asyncio",
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
    name = "cluster_summarize_test",
    srcs = ["summarize_test.py"],
    imports = [".."],
    deps = [
        ":cluster",
        "@pip//pytest",
    ],
)

py_test(
    name = "cluster_mcp_test",
    srcs = ["mcp_test.py"],
    imports = [".."],
    deps = [
        ":cluster",
        "//projects/monolith/core",
        "@pip//fastmcp",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "cluster_kubernetes_test",
    srcs = ["kubernetes_test.py"],
    imports = [".."],
    deps = [
        ":cluster",
        "@pip//kubernetes_asyncio",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "cluster_cd_health_test",
    srcs = ["cd_health_test.py"],
    imports = [".."],
    deps = [
        ":cluster",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "cluster_recovery_startup_test",
    srcs = ["recovery_startup_test.py"],
    imports = [".."],
    deps = [
        ":cluster",
        "//projects/monolith:pkg_cyclegroup",
        "//projects/monolith/framework",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
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
