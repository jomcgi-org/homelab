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
    name = "ember_public",
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
        "//projects/monolith/chat_public",
        "//projects/monolith/core",
        "//projects/monolith/faas",
        "//projects/monolith/framework",
        "//projects/monolith/semgrep_scan",
        "//projects/monolith/shared",
        "@pip//certifi",
        "@pip//fastapi",
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
    name = "ember_public_router_test",
    srcs = ["router_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "ember_public_bazel_core_test",
    srcs = ["bazel_core_test.py"],
    imports = [".."],
    deps = [
        ":ember_public",
        "//projects/monolith/faas",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "ember_public_health_test",
    srcs = ["health_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "ember_public_synthetic_health_test",
    srcs = ["synthetic_health_test.py"],
    imports = [".."],
    deps = [
        ":ember_public",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "ember_public_synthetic_probe_test",
    srcs = ["synthetic_probe_test.py"],
    imports = [".."],
    deps = [
        # probe_codex imports agent_sessions.api lazily, inside the function, so
        # the public binary never pulls it in at module load. The private probe
        # endpoint has the package at runtime, and this test needs the module on
        # its path so monkeypatch can resolve run_synthetic_session.
        "//projects/monolith/agent_sessions",
        ":ember_public",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "ember_public_synthetic_router_test",
    srcs = ["synthetic_router_test.py"],
    imports = [".."],
    deps = [
        ":ember_public",
        "@pip//fastapi",
        "@pip//httpx",
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
            "synthetic_probe.py",
            "synthetic_router.py",
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
