# gazelle:ignore
# gazelle:exclude backfill
# gazelle:exclude testdata

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("@aspect_rules_py//py/private/py_venv:defs.bzl", "py_venv_binary")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_target_test")

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
            "backfill/**/*.py",
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
    name = "pkg_trips",
    srcs = glob(
        [
            "**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "tests/**/*.py",
            "backfill/**/*.py",
        ],
    ),
    imports = [".."],
    tags = ["monolith-private"],
    visibility = ["//projects/monolith:package_consumers"],
    deps = [
        "//projects/monolith/core:pkg_core",
        "//projects/monolith/framework:pkg_framework",
        "@pip//boto3",
        "@pip//certifi",
        "@pip//defusedxml",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//opentelemetry_exporter_otlp_proto_http",
        "@pip//opentelemetry_instrumentation_fastapi",
        "@pip//opentelemetry_instrumentation_httpx",
        "@pip//opentelemetry_sdk",
        "@pip//pgvector",
        "@pip//pillow",
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

py_library(
    name = "public",
    srcs = glob(
        ["**/*.py"],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "tests/**/*.py",
            "backfill/**/*.py",
            "ingest_router.py",
            "ingest.py",
            "s3.py",
            "exif.py",
            "transform.py",
        ],
    ),
    imports = [".."],
    tags = ["monolith-public"],
    visibility = ["//projects/monolith/public_backend:__pkg__"],
)

py_library(
    name = "trips_backfill_lib",
    srcs = glob(
        ["**/*.py"],
        exclude = ["**/*_test.py"],
    ),
    imports = [".."],
    visibility = ["//projects/monolith:package_consumers"],
    deps = [
        "@pip//boto3",
        "@pip//botocore",
        "@pip//defusedxml",
        "@pip//httpx",
        "@pip//pillow",
        "@pip//pyyaml",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
        "@pip//typer",
        "@pip//tzdata",
    ],
)

py_venv_binary(
    name = "trips_backfill",
    srcs = glob(
        ["**/*.py"],
        exclude = ["**/*_test.py"],
    ),
    main = "backfill/main.py",
    visibility = ["//projects/monolith:package_consumers"],
)

semgrep_target_test(
    name = "trips_backfill_semgrep_test",
    exclude_rules = [
        # main.py guards the SeaweedFS endpoint with a scheme check, but
        # semgrep-core ignores the inline # nosemgrep (mirrors :main). The
        # # keep stops gazelle from dropping it back to the file directive set.
        "boto3-endpoint-url-missing-scheme",  # keep
        # trips.models uses datetime fields for the migration's TIMESTAMPTZ
        # columns; semgrep-core ignores the inline # nosemgrep (mirrors :main).
        "sqlmodel-datetime-without-factory",
        "avoid-sqlalchemy-text",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-stdlib-fastapi",
        # validate_image opens in-memory bytes, not a path; false positive (mirrors :main).
        "tainted-path-traversal-pillow-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
    target = ":trips_backfill",
)

filegroup(
    name = "testdata",
    srcs = glob(["testdata/**"]),
    visibility = ["//projects/monolith:__pkg__"],
)
