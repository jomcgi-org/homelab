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

# keep: recursive sources and reviewed runtime dependencies are package-owned.# COUNT 36

py_library(
    name = "scheduler",
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
        "//projects/monolith/cluster",
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
    name = "scheduler_test",
    srcs = ["scheduler_test.py"],
    imports = [".."],
    deps = [
        ":scheduler",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "scheduler_argo_handled_test",
    srcs = ["argo_handled_test.py"],
    imports = [".."],
    deps = [
        ":scheduler",
        "@pip//pytest",
    ],
)

py_test(
    name = "scheduler_views_test",
    srcs = ["views_test.py"],
    imports = [".."],
    deps = [
        ":scheduler",
        "@pip//pydantic",
        "@pip//pytest",
    ],
)

py_test(
    name = "scheduler_service_test",
    srcs = ["service_test.py"],
    imports = [".."],
    deps = [
        ":scheduler",
        "@pip//fastapi",
        "@pip//kubernetes_asyncio",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "scheduler_router_test",
    srcs = ["router_test.py"],
    imports = [".."],
    deps = [
        ":scheduler",
        "//projects/monolith/core",
        "//projects/monolith/framework",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
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
