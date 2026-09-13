# gazelle:ignore
# gazelle:exclude testdata

load("@aspect_rules_py//py:defs.bzl", "py_library")

package(default_visibility = ["//visibility:private"])

# Root compatibility tests still own their hand-written rule metadata. Export
# the files, but keep source membership and tier classification in this package.
exports_files(
    ["BUILD"] + glob(
        ["**/*"],
        exclude = ["BUILD"],
    ),
    visibility = ["//projects/monolith:__pkg__"],
)

filegroup(
    name = "runtime_sources",
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

# Source inventories used by root-level static tests. These deliberately differ
# from runtime_sources: boundary scans retain offline modules, while the broad
# source scan retains non-test helpers under tests/ but excludes conftest.py.
filegroup(
    name = "boundary_sources",
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
    name = "source_scan_files",
    srcs = glob(
        ["**/*.py"],
        exclude = [
            "**/*_test.py",
            "**/conftest.py",
            "**/test_*.py",
        ],
    ),
    visibility = ["//projects/monolith:__pkg__"],
)

filegroup(
    name = "bdd_tests",
    srcs = glob(
        ["tests/*_test.py"],
        allow_empty = True,
    ),
    visibility = ["//projects/monolith:__pkg__"],
)

py_library(
    name = "pkg_semgrep_scan",
    srcs = glob(
        [
            "**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "tests/**/*.py",
        ],
    ),
    imports = [".."],
    tags = ["monolith-private"],
    visibility = ["//projects/monolith:package_consumers"],
    deps = [
        "//projects/monolith/core:pkg_core",
        "//projects/monolith/framework:pkg_framework",
        "//projects/monolith/shared:pkg_shared",
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

py_library(
    name = "public",
    srcs = glob(
        ["**/*.py"],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "tests/**/*.py",
            "mcp.py",
            "module.py",
            "report.py",
            "router.py",
            "cohorts.py",
            "perf_*.py",
        ],
    ),
    imports = [".."],
    tags = ["monolith-public"],
    visibility = ["//projects/monolith/public_backend:__pkg__"],
)
