# Bazel-native package ownership. Classification is declared here, not in
# projects/monolith/BUILD.

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

package(default_visibility = ["//visibility:private"])

exports_files(
    glob(["**/*.py"]),
    visibility = ["//projects/monolith:private_backend_consumers"],
)

py_library(
    name = "semgrep_scan",
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
        # NOTE: semgrep_scan/report.py uploads to the Semgrep App via plain
        # authenticated HTTP (explicit Authorization: Bearer) using httpx (already
        # a dep above), not ScanHandler.
        # pysemgrep INTERNAL APIs for the fc-invoke -> Semgrep-App relay
        # (semgrep_scan/report.py): only the out.* atd types (light) + __VERSION__.
        # We do NOT use ScanHandler / get_state / app_session (auth never attaches
        # outside a click command, and start_scan polls the OOM /config ruleset).
        # Pinned 1.168.0 in bazel/requirements/tools.in.
        "@pip//semgrep",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
        "@pip//tzdata",
        "@pip//uvicorn",
    ],
)

py_test(
    name = "semgrep_mcp_test",
    srcs = ["mcp_test.py"],
    imports = [".."],
    deps = [
        ":semgrep_scan",
        "//projects/monolith/core",
        "@pip//fastmcp",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "semgrep_report_test",
    srcs = ["report_test.py"],
    data = [
        # report_test loads this captured cli_output fixture at runtime via
        # Path(__file__).parent/"testdata"; the "**/*.py" backend
        # glob does not capture the .json, so ship it into runfiles. 8 results
        # (7 findings + 1 ignored), derived from a real 1.168.0 Pro finding.
        "testdata/real_cli_output.json",
    ],
    imports = [".."],
    # Drives the async relay via asyncio.run (sync test fns), so no
    # pytest-asyncio plugin is needed.
    deps = [
        ":semgrep_scan",
        "@pip//pytest",
        # httpx: the test mocks semgrep_scan.report.httpx.post to capture the three
        # App uploads (CREATE + /results + /complete) and their Bearer auth.
        "@pip//httpx",
        # pysemgrep out.* atd types (light). Pinned to 1.168.0 in
        # bazel/requirements/tools.in to match the fc-invoke Pro engine that
        # produced the fixture. NOT ScanHandler / app_session.
        "@pip//semgrep",
    ],
)

py_test(
    name = "semgrep_router_test",
    srcs = ["router_test.py"],
    imports = [".."],
    # gazelle:exclude semgrep_scan means this py_test is hand-added, not generated.
    # Drives the async webhook via FastAPI TestClient (BackgroundTasks run inline
    # after the response), mocking scan_files / report_pr_scan / httpx so no live
    # GitHub, fc-invoke, or Semgrep App is touched.
    deps = [
        ":semgrep_scan",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "semgrep_perf_store_test",
    srcs = ["perf_store_test.py"],
    imports = [".."],
    # gazelle:exclude semgrep_scan means this py_test is hand-added, not generated.
    deps = [
        ":semgrep_scan",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "semgrep_perf_harvest_test",
    srcs = ["perf_harvest_test.py"],
    imports = [".."],
    # gazelle:exclude semgrep_scan means this py_test is hand-added, not generated.
    deps = [
        ":semgrep_scan",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "semgrep_cohorts_test",
    srcs = ["cohorts_test.py"],
    imports = [".."],
    # gazelle:exclude semgrep_scan means this py_test is hand-added, not generated.
    deps = [
        ":semgrep_scan",
        "@pip//pytest",
    ],
)

py_test(
    name = "semgrep_perf_compare_test",
    srcs = ["perf_compare_test.py"],
    imports = [".."],
    # gazelle:exclude semgrep_scan means this py_test is hand-added, not generated.
    deps = [
        ":semgrep_scan",
        "@pip//pytest",
    ],
)

py_test(
    name = "semgrep_perf_router_test",
    srcs = ["perf_router_test.py"],
    imports = [".."],
    # gazelle:exclude semgrep_scan means this py_test is hand-added, not generated.
    deps = [
        ":semgrep_scan",
        "//projects/monolith/core",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "semgrep_perf_webhook_test",
    srcs = ["perf_webhook_test.py"],
    imports = [".."],
    # gazelle:exclude semgrep_scan means this py_test is hand-added, not generated.
    deps = [
        ":semgrep_scan",
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
            "mcp.py",
            "module.py",
            "report.py",
            "router.py",
            "cohorts.py",
            "perf_*.py",
            "**/test_*.py",
            "tests/**/*.py",
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
