load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")

# Bazel-native package ownership. Classification is declared here, not in
# projects/monolith/BUILD.
# Every Python test in this package has an explicit target below. Keep
# Gazelle from generating duplicate per-file py_test rules.
# gazelle:exclude *_test.py
# gazelle:exclude test_*.py
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
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//opentelemetry_api",
        "@pip//pydantic",
        "@pip//sqlmodel",
        "@pip//starlette",
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

semgrep_test(
    name = "__init___semgrep_test",
    srcs = ["__init__.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "admission_semgrep_test",
    srcs = ["admission.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "api_semgrep_test",
    srcs = ["api.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "codex_login_semgrep_test",
    srcs = ["codex_login.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "constants_semgrep_test",
    srcs = ["constants.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "execution_api_semgrep_test",
    srcs = ["execution_api.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "factory_public_semgrep_test",
    srcs = ["factory_public.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "factory_view_semgrep_test",
    srcs = ["factory_view.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "kg_feed_semgrep_test",
    srcs = ["kg_feed.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "mcp_semgrep_test",
    srcs = ["mcp.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "models_semgrep_test",
    srcs = ["models.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "module_semgrep_test",
    srcs = ["module.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "permit_supervision_semgrep_test",
    srcs = ["permit_supervision.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "progress_ingest_semgrep_test",
    srcs = ["progress_ingest.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "provider_quota_semgrep_test",
    srcs = ["provider_quota.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "public_module_semgrep_test",
    srcs = ["public_module.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "public_router_semgrep_test",
    srcs = ["public_router.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "rationale_semgrep_test",
    srcs = ["rationale.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "reconciliation_semgrep_test",
    srcs = ["reconciliation.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "redact_semgrep_test",
    srcs = ["redact.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "result_receipts_semgrep_test",
    srcs = ["result_receipts.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "result_receipts_router_semgrep_test",
    srcs = ["result_receipts_router.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "router_semgrep_test",
    srcs = ["router.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "store_semgrep_test",
    srcs = ["store.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "titles_semgrep_test",
    srcs = ["titles.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "transport_semgrep_test",
    srcs = ["transport.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "voice_semgrep_test",
    srcs = ["voice.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_test(
    name = "voice_ui_semgrep_test",
    srcs = ["voice_ui.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)
