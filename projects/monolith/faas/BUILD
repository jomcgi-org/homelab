# Bazel-native package ownership. Classification is declared here, not in
# projects/monolith/BUILD.
# Every Python test in this package has an explicit target below. Keep
# Gazelle from generating duplicate per-file py_test rules.
# gazelle:exclude *_test.py
# gazelle:exclude test_*.py
# Nested sources remain owned by this package's recursive targets.
# gazelle:exclude functions

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

package(default_visibility = ["//visibility:private"])

exports_files(
    glob(["**/*.py"]),
    visibility = ["//projects/monolith:private_backend_consumers"],
)

py_library(
    name = "faas",
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
        "//projects/monolith/shared",
        "@pip//boto3",
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
    name = "faas_models_test",
    srcs = ["models_test.py"],
    imports = [".."],
    deps = [
        ":faas",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "faas_storage_test",
    srcs = ["storage_test.py"],
    imports = [".."],
    deps = [
        ":faas",
        "@pip//boto3",
        "@pip//pytest",
    ],
)

py_test(
    name = "faas_runtime_test",
    srcs = ["runtime_test.py"],
    imports = [".."],
    deps = [
        ":faas",
        "@pip//pytest",
    ],
)

py_test(
    name = "faas_embervm_client_test",
    srcs = ["embervm_client_test.py"],
    imports = [".."],
    deps = [
        ":faas",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "faas_workload_test",
    srcs = ["workload_test.py"],
    imports = [".."],
    deps = [
        ":faas",
        "@pip//kubernetes_asyncio",
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

py_test(
    name = "faas_reconcile_test",
    srcs = ["reconcile_test.py"],
    imports = [".."],
    deps = [
        ":faas",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "faas_router_test",
    srcs = ["router_test.py"],
    imports = [".."],
    deps = [
        ":faas",
        "//projects/monolith/core",
        "//projects/monolith/framework",
        "@pip//fastapi",
        "@pip//pytest",
        "@pip//python_multipart",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "faas_invoke_router_test",
    srcs = ["invoke_router_test.py"],
    imports = [".."],
    deps = [
        ":faas",
        "//projects/monolith/core",
        "//projects/monolith/framework",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "faas_invoke_router_public_test",
    srcs = ["invoke_router_public_test.py"],
    imports = [".."],
    deps = [
        ":faas",
        "//projects/monolith/core",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
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
            "router.py",
            "storage.py",
            "workload.py",
            "functions/**/*.py",
        ],
    ),
    imports = [".."],
    tags = ["monolith-public"],
    visibility = ["//projects/monolith/public_backend:__pkg__"],
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
