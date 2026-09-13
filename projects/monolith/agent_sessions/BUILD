# Bazel-native package ownership. Classification is declared here, not in
# projects/monolith/BUILD.
# These tests use the BDD harness or share one process-crash driver target and
# are owned explicitly below, so Gazelle must not generate parallel py_tests.
# gazelle:exclude factory_public_writer_test.py
# gazelle:exclude process_crash_driver_test.py
# gazelle:exclude process_crash_test.py
# gazelle:exclude public_agent_activity_views_test.py
# gazelle:exclude store_postgres_test.py

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")
load("//projects/monolith:bdd_test.bzl", "bdd_test")

package(default_visibility = ["//visibility:private"])

exports_files(
    glob(["**/*.py"]),
    visibility = ["//projects/monolith:private_backend_consumers"],
)

py_library(
    name = "agent_sessions_rationale",
    srcs = ["rationale.py"],
    imports = [".."],
    visibility = ["//projects/monolith:private_backend_consumers"],
)

py_library(
    name = "agent_sessions_admission",
    srcs = [
        "__init__.py",
        "admission.py",
        "api.py",
        "constants.py",
        "models.py",
        "reconciliation.py",
    ],
    imports = [".."],
    visibility = ["//projects/monolith:private_backend_consumers"],
    deps = [
        "//projects/monolith/core",
        "//projects/monolith/shared",
        "//projects/monolith/swarm:swarm_factory_controls",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_library(
    name = "agent_sessions",
    srcs = glob(
        [
            "**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
            "rationale.py",
            "__init__.py",
            "admission.py",
            "api.py",
            "constants.py",
            "models.py",
            "reconciliation.py",
        ],
    ),
    imports = [".."],
    tags = ["monolith-backend"],
    visibility = ["//projects/monolith:private_backend_consumers"],
    deps = [
        ":agent_sessions_admission",
        ":agent_sessions_rationale",
        # mcp.py and router.py read the request principal via auth.api.
        "//projects/monolith/auth",
        "//projects/monolith/core",
        "//projects/monolith:pkg_cyclegroup",
        "//projects/monolith/faas",
        "//projects/monolith/framework",
        "//projects/monolith/knowledge",
        "//projects/monolith/shared",
        "//projects/monolith/swarm:swarm_factory_controls",
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
    name = "agent_sessions_router_test",
    srcs = ["router_test.py"],
    imports = [".."],
    deps = [
        ":agent_sessions",
        "//projects/monolith/core",
        # The factory decision route hands the work to swarm.factory_decisions.
        "//projects/monolith/swarm",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_factory_transport_test",
    srcs = ["factory_transport_test.py"],
    imports = [".."],
    deps = [
        ":agent_sessions",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "agent_sessions_permit_supervision_test",
    srcs = ["permit_supervision_test.py"],
    imports = [".."],
    deps = [
        ":agent_sessions",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_factory_view_test",
    srcs = ["factory_view_test.py"],
    imports = [".."],
    deps = [
        ":agent_sessions",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_factory_public_test",
    srcs = ["factory_public_test.py"],
    imports = [".."],
    deps = [
        ":agent_sessions",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_admission_test",
    srcs = ["admission_test.py"],
    imports = [".."],
    deps = [
        ":agent_sessions",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_store_test",
    srcs = ["store_test.py"],
    imports = [".."],
    deps = [
        ":agent_sessions",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

bdd_test(
    name = "agent_sessions_store_postgres_test",
    srcs = ["store_postgres_test.py"],
)

bdd_test(
    name = "agent_sessions_process_crash_test",
    srcs = [
        "process_crash_driver_test.py",
        "process_crash_test.py",
    ],
)

py_test(
    name = "agent_sessions_api_test",
    srcs = ["api_test.py"],
    imports = [".."],
    deps = [
        ":agent_sessions",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_progress_ingest_test",
    srcs = ["progress_ingest_test.py"],
    imports = [".."],
    deps = [
        ":agent_sessions",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_result_receipts_test",
    srcs = ["result_receipts_test.py"],
    imports = [".."],
    deps = [
        ":agent_sessions",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_preinvoke_test",
    srcs = ["preinvoke_test.py"],
    imports = [".."],
    deps = [
        ":agent_sessions",
        "//projects/monolith:pkg_cyclegroup",
        "//projects/monolith/core",
        "//projects/monolith/knowledge",
        "//projects/monolith/swarm",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_receipt_capture_test",
    srcs = ["receipt_capture_test.py"],
    imports = [".."],
    deps = [
        ":agent_sessions",
        "//projects/monolith/core",
        "//projects/monolith/swarm",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_receipt_consumer_test",
    srcs = ["receipt_consumer_test.py"],
    imports = [".."],
    deps = [
        ":agent_sessions",
        "//projects/monolith/core",
        "//projects/monolith/swarm",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_response_lost_test",
    srcs = ["response_lost_test.py"],
    imports = [".."],
    deps = [
        ":agent_sessions",
        "//projects/monolith/core",
        "//projects/monolith/swarm",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

bdd_test(
    name = "public_agent_activity_views_test",
    srcs = ["public_agent_activity_views_test.py"],
    deps = ["@pip//typer"],
)

bdd_test(
    name = "factory_public_writer_test",
    srcs = ["factory_public_writer_test.py"],
)

py_test(
    name = "agent_sessions_store_voice_test",
    srcs = ["store_voice_test.py"],
    imports = [".."],
    deps = [
        ":agent_sessions",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_titles_test",
    srcs = ["titles_test.py"],
    imports = [".."],
    deps = [
        ":agent_sessions",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_kg_feed_test",
    srcs = ["kg_feed_test.py"],
    imports = [".."],
    deps = [
        ":agent_sessions",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_mcp_test",
    srcs = ["mcp_test.py"],
    imports = [".."],
    deps = [
        ":agent_sessions",
        "@pip//fastmcp",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_provider_quota_test",
    srcs = ["provider_quota_test.py"],
    imports = [".."],
    deps = [
        ":agent_sessions",
        "//projects/monolith/framework",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_transport_test",
    srcs = ["transport_test.py"],
    imports = [".."],
    deps = [
        ":agent_sessions",
        "@pip//httpx",
        "@pip//opentelemetry_sdk",
        "@pip//pytest",
    ],
)

py_test(
    name = "agent_sessions_voice_ui_test",
    srcs = ["voice_ui_test.py"],
    imports = [".."],
    deps = [
        ":agent_sessions",
        # The companion routes and the voice_ui_* tools read the request
        # principal, so the test drives auth.dependencies directly.
        "//projects/monolith/auth",
        "//projects/monolith/core",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_swarm_decision_test",
    srcs = ["swarm_decision_test.py"],
    imports = [".."],
    deps = [
        ":agent_sessions",
        "//projects/monolith/auth",
        "//projects/monolith/swarm",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_public_agent_activity_router_test",
    srcs = ["public_agent_activity_router_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:monolith_public_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
    ],
)

py_test(
    name = "agent_sessions_public_factory_router_test",
    srcs = ["public_factory_router_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:monolith_public_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
    ],
)

# keep
py_library(
    name = "public",
    srcs = glob(
        [
            "__init__.py",
            "public_module.py",
            "public_router.py",
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
