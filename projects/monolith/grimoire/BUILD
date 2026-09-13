# Bazel-native package ownership. Classification is declared here, not in
# projects/monolith/BUILD.
# The root monolith Semgrep target already scans this package transitively.
# gazelle:semgrep disabled
# Every Python test in this package has an explicit target below. Keep
# Gazelle from generating duplicate per-file py_test rules.
# gazelle:exclude *_test.py
# gazelle:exclude test_*.py
# The explicit marker target owns this executable entrypoint.
# gazelle:exclude marker.py
# Nested sources remain owned by this package's recursive targets.
# gazelle:exclude tools

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

package(default_visibility = ["//visibility:private"])

exports_files(
    glob(["**/*.py"]),
    visibility = ["//projects/monolith:private_backend_consumers"],
)

# keep: recursive sources and reviewed runtime dependencies are package-owned.
py_library(
    name = "grimoire",
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
        "//projects/monolith/knowledge",
        "//projects/monolith/scheduler",
        "//projects/monolith/shared",
        "@pip//boto3",
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
    name = "grimoire_models_test",
    srcs = ["models_test.py"],
    imports = [".."],
    deps = [
        ":grimoire",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "grimoire_visibility_test",
    srcs = ["visibility_test.py"],
    imports = [".."],
    deps = [
        ":grimoire",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "grimoire_router_test",
    srcs = ["router_test.py"],
    imports = [".."],
    deps = [
        ":grimoire",
        "//projects/monolith/core",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "grimoire_entities_test",
    srcs = ["entities_test.py"],
    imports = [".."],
    deps = [
        ":grimoire",
        "//projects/monolith/core",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "grimoire_search_test",
    srcs = ["search_test.py"],
    imports = [".."],
    deps = [
        ":grimoire",
        "//projects/monolith/core",
        "//projects/monolith/knowledge",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "grimoire_library_test",
    srcs = ["library_test.py"],
    imports = [".."],
    deps = [
        ":grimoire",
        "//projects/monolith/core",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "grimoire_public_test",
    srcs = ["public_test.py"],
    imports = [".."],
    deps = [
        ":grimoire",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "grimoire_explore_test",
    srcs = ["explore_test.py"],
    imports = [".."],
    deps = [
        ":grimoire",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "grimoire_router_public_test",
    srcs = ["router_public_test.py"],
    imports = [".."],
    deps = [
        ":grimoire",
        "//projects/monolith/core",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "grimoire_ingest_test",
    srcs = ["ingest_test.py"],
    imports = [".."],
    deps = [
        ":grimoire",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "grimoire_extract_test",
    srcs = ["extract_test.py"],
    imports = [".."],
    deps = [
        ":grimoire",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "grimoire_marker_test",
    srcs = ["marker_test.py"],
    imports = [".."],
    deps = [
        ":grimoire",
        "@pip//pytest",
    ],
)

py_test(
    name = "grimoire_jobs_test",
    srcs = ["jobs_test.py"],
    imports = [".."],
    deps = [
        ":grimoire",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "grimoire_hierarchy_backfill_test",
    srcs = ["hierarchy_backfill_test.py"],
    imports = [".."],
    deps = [
        ":grimoire",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
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
            "jobs.py",
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
