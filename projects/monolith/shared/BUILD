# Bazel-native package ownership. Classification is declared here, not in
# projects/monolith/BUILD.
# The root monolith Semgrep target already scans this package transitively.
# gazelle:semgrep disabled
# Every Python test in this package has an explicit target below. Keep
# Gazelle from generating duplicate per-file py_test rules.
# gazelle:exclude *_test.py
# gazelle:exclude test_*.py
# Nested sources remain owned by this package's recursive targets.
# gazelle:exclude testing

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

package(default_visibility = ["//visibility:private"])

exports_files(
    glob(["**/*.py"]),
    visibility = ["//projects/monolith:private_backend_consumers"],
)

# Recursive sources and reviewed runtime dependencies are package-owned.
# keep
py_library(
    name = "shared",
    srcs = glob(
        [
            "**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
            "testing/**/*.py",
        ],
    ),
    imports = [".."],
    tags = ["monolith-backend"],
    visibility = ["//projects/monolith:private_backend_consumers"],
    deps = [
        "@pip//certifi",
        "@pip//fastapi",
        "@pip//genai_prices",
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

# Recursive sources and reviewed runtime dependencies are package-owned.
# keep
py_library(
    name = "shared_testing",
    srcs = glob(["testing/**/*.py"]),
    imports = [".."],
    visibility = ["//projects/monolith:private_backend_consumers"],
    deps = [
        "//projects/monolith:monolith_backend",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "markers_test",
    srcs = ["testing/markers_test.py"],
    imports = [".."],
    deps = [
        ":shared_testing",
        "@pip//pytest",
    ],
)

py_test(
    name = "shared_embedding_test",
    srcs = ["embedding_test.py"],
    imports = [".."],
    deps = [
        ":shared",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "shared_pricing_test",
    srcs = ["pricing_test.py"],
    imports = [".."],
    deps = [
        ":shared",
        "@pip//pytest",
    ],
)

py_test(
    name = "shared_inference_test",
    srcs = ["inference_test.py"],
    imports = [".."],
    deps = [
        ":shared",
        "@pip//opentelemetry_sdk",
        "@pip//pytest",
    ],
)

py_test(
    name = "shared_k8s_auth_test",
    srcs = ["k8s_auth_test.py"],
    imports = [".."],
    deps = [
        ":shared",
        "@pip//pytest",
    ],
)

py_test(
    name = "shared_embedding_retry_test",
    srcs = ["embedding_retry_test.py"],
    imports = [".."],
    deps = [
        ":shared",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "shared_embedding_batch_test",
    srcs = ["embedding_batch_test.py"],
    imports = [".."],
    deps = [
        ":shared",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "shared_embedding_coverage_test",
    srcs = ["embedding_coverage_test.py"],
    imports = [".."],
    deps = [
        ":shared",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "shared_embedding_extra_test",
    srcs = ["embedding_extra_test.py"],
    imports = [".."],
    deps = [
        ":shared",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "shared_embedding_null_test",
    srcs = ["embedding_null_test.py"],
    imports = [".."],
    deps = [
        ":shared",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "shared_embedding_retryable_test",
    srcs = ["embedding_retryable_test.py"],
    imports = [".."],
    deps = [
        ":shared",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "shared_embedding_connect_timeout_test",
    srcs = ["embedding_connect_timeout_test.py"],
    imports = [".."],
    deps = [
        ":shared",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "shared_forecast_freshness_test",
    srcs = ["forecast_freshness_test.py"],
    imports = [".."],
    deps = [
        ":shared",
        "@pip//pytest",
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
            "testing/**/*.py",
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
            "testing/**/*.py",
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
            "testing/**/*.py",
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
