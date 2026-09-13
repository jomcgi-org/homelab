# Bazel-native package ownership. Classification is declared here, not in
# projects/monolith/BUILD.
# The root monolith Semgrep target already scans this package transitively.
# gazelle:semgrep disabled
# Every Python test in this package has an explicit target below. Keep
# Gazelle from generating duplicate per-file py_test rules.
# gazelle:exclude *_test.py
# gazelle:exclude test_*.py
# Nested sources remain owned by this package's recursive targets.
# gazelle:exclude tests

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
    name = "goosecracker",
    srcs = glob(
        ["**/*.py"],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "tests/**/*.py",
        ],
    ),
    imports = [".."],
    tags = ["monolith-backend"],
    visibility = ["//projects/monolith:private_backend_consumers"],
    deps = [
        "//projects/monolith/agent_sessions:agent_sessions_admission",
        "//projects/monolith/artifact",
        "//projects/monolith/cluster",
        "//projects/monolith/core",
        "//projects/monolith/framework",
        "//projects/monolith/knowledge",
        "//projects/monolith/sandbox",
        "//projects/monolith/scheduler",
        "//projects/monolith/shared",
        "@pip//boto3",
        "@pip//certifi",
        "@pip//discord_py",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//icalendar",
        "@pip//opentelemetry_exporter_otlp_proto_http",
        "@pip//opentelemetry_instrumentation_fastapi",
        "@pip//opentelemetry_instrumentation_httpx",
        "@pip//opentelemetry_sdk",
        "@pip//pgvector",
        "@pip//psycopg",
        "@pip//psycopg_binary",
        "@pip//pydantic",
        "@pip//pydantic_ai_slim",
        "@pip//python_multipart",
        "@pip//pyyaml",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
        "@pip//tzdata",
        "@pip//uvicorn",
    ],
)

py_test(
    name = "goosecracker_repo_catalog_test",
    srcs = ["tests/repo_catalog_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "goosecracker_recipe_catalog_test",
    srcs = ["tests/recipe_catalog_test.py"],
    data = glob(["recipes/*.yaml"]),
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
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
