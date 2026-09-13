# Bazel-native package ownership. Classification is declared here, not in
# projects/monolith/BUILD.
# The root monolith Semgrep target already scans this package transitively.
# gazelle:semgrep disabled
# Every Python test in this package has an explicit target below. Keep
# Gazelle from generating duplicate per-file py_test rules.
# gazelle:exclude *_test.py
# gazelle:exclude test_*.py
# Nested sources remain owned by this package's recursive targets.
# gazelle:exclude observability
# gazelle:exclude tests
# These suites use the BDD harness and are owned by explicit bdd_test targets.
# gazelle:exclude tests/bdd_api_test.py
# gazelle:exclude tests/bdd_playwright_test.py
# gazelle:exclude tests/bdd_public_test.py

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")
load("//projects/monolith:bdd_test.bzl", "bdd_test")

package(default_visibility = ["//visibility:private"])

exports_files(
    glob(["**/*.py"]),
    visibility = ["//projects/monolith:private_backend_consumers"],
)

# keep: recursive sources and reviewed runtime dependencies are package-owned.
py_library(
    name = "home",
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
    name = "home_schedule_test",
    srcs = ["schedule_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//httpx",
        "@pip//icalendar",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//tzdata",
    ],
)

py_test(
    name = "home_schedule_extra_test",
    srcs = ["schedule_extra_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//httpx",
        "@pip//icalendar",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//tzdata",
    ],
)

py_test(
    name = "home_schedule_router_test",
    srcs = ["schedule_router_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "//projects/monolith/framework",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//tzdata",
    ],
)

py_test(
    name = "home_dashboard_test",
    srcs = ["dashboard_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:monolith_backend",
        "//projects/monolith:monolith_public_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
        "@pip//tzdata",
    ],
)

py_test(
    name = "home_cluster_snapshot_test",
    srcs = ["cluster_snapshot_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlalchemy",
    ],
)

py_test(
    name = "home_schedule_calendar_handler_test",
    srcs = ["schedule_calendar_handler_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//icalendar",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//tzdata",
    ],
)

py_test(
    name = "home_schedule_naive_dtend_test",
    srcs = ["schedule_naive_dtend_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//icalendar",
        "@pip//pytest",
        "@pip//tzdata",
    ],
)

py_test(
    name = "home_observability_slo_test",
    srcs = ["observability/slo_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "home_observability_router_test",
    srcs = ["observability/router_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "//projects/monolith/core",
        "//projects/monolith/framework",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "home_observability_stats_test",
    srcs = ["observability/stats_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "home_observability_traces_test",
    srcs = ["observability/traces_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "home_observability_rollup_test",
    srcs = ["observability/rollup_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

bdd_test(
    name = "home_bdd_api_test",
    srcs = [
        "tests/__init__.py",
        "tests/bdd_api_test.py",
        "tests/conftest.py",
    ],
)

bdd_test(
    name = "home_bdd_playwright_test",
    timeout = "long",
    srcs = [
        "tests/__init__.py",
        "tests/bdd_playwright_test.py",
        "tests/conftest.py",
    ],
    playwright = True,
)

bdd_test(
    name = "home_bdd_public_test",
    srcs = [
        "tests/__init__.py",
        "tests/bdd_public_test.py",
        "tests/conftest.py",
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
            "schedule.py",
            "schedule_router.py",
            "observability/slo.py",
            "observability/rollup.py",
            "observability/stats.py",
            "observability/traces.py",
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
