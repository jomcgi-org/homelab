# Bazel-native package ownership. Classification is declared here, not in
# projects/monolith/BUILD.
# Every Python test in this package has an explicit target below. Keep
# Gazelle from generating duplicate per-file py_test rules.
# gazelle:exclude *_test.py
# gazelle:exclude test_*.py
# Nested sources remain owned by this package's recursive targets.
# gazelle:exclude backfill

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("@aspect_rules_py//py/private/py_venv:defs.bzl", "py_venv_binary")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_target_test")
load("//bazel/tools/pytest:defs.bzl", "py_test")

package(default_visibility = ["//visibility:private"])

exports_files(
    glob(["**/*.py"]),
    visibility = ["//projects/monolith:private_backend_consumers"],
)

# Run-by-hand backfill, excluded from the server image.
py_venv_binary(
    name = "trips_backfill",
    srcs = glob(
        ["**/*.py"],
        exclude = ["**/*_test.py"],
    ),
    imports = [".."],
    main = "backfill/main.py",
    visibility = ["//projects/monolith:__pkg__"],
)

semgrep_target_test(
    name = "trips_backfill_semgrep_test",
    exclude_rules = [
        "boto3-endpoint-url-missing-scheme",  # keep
        "sqlmodel-datetime-without-factory",
        "avoid-sqlalchemy-text",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-stdlib-fastapi",
        "tainted-path-traversal-pillow-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
    target = ":trips_backfill",
)

py_library(
    name = "trips",
    srcs = glob(
        [
            "**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
            "backfill/**",
        ],
    ),
    imports = [".."],
    tags = ["monolith-backend"],
    visibility = ["//projects/monolith:private_backend_consumers"],
    deps = [
        "//projects/monolith/core",
        "//projects/monolith/framework",
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
    name = "trips_backfill_lib",
    srcs = glob(
        ["**/*.py"],
        exclude = ["**/*_test.py"],
    ),
    imports = [".."],
    visibility = ["//projects/monolith:private_backend_consumers"],
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

py_test(
    name = "trips_backfill_main_test",
    srcs = ["backfill/main_test.py"],
    imports = [".."],
    deps = [
        ":trips_backfill_lib",
        "@pip//pytest",
    ],
)

py_test(
    name = "trips_models_test",
    srcs = ["models_test.py"],
    imports = [".."],
    deps = [
        ":trips",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "trips_transform_test",
    srcs = ["transform_test.py"],
    imports = [".."],
    deps = [
        ":trips",
        "@pip//pytest",
    ],
)

py_test(
    name = "trips_exif_test",
    srcs = ["exif_test.py"],
    imports = [".."],
    deps = [
        ":trips",
        "@pip//pytest",
    ],
)

py_test(
    name = "trips_ingest_test",
    srcs = ["ingest_test.py"],
    # Tiny committed JPEG fixtures (geotagged + no-GPS) the test reads through
    # the real exif.extract_exif path. Excluded from the server libs above so
    # the bytes never ship in the production image.
    data = glob(["testdata/**"]),
    imports = [".."],
    deps = [
        ":trips",
        "@pip//pytest",
    ],
)

py_test(
    name = "trips_ingest_router_test",
    srcs = ["ingest_router_test.py"],
    # Same committed JPEG fixtures posted through the TestClient.
    data = glob(["testdata/**"]),
    imports = [".."],
    deps = [
        ":trips",
        "//projects/monolith/core",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//python_multipart",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "trips_read_router_test",
    srcs = ["read_router_test.py"],
    imports = [".."],
    deps = [
        ":trips",
        "//projects/monolith/core",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "trips_s3_test",
    srcs = ["s3_test.py"],
    imports = [".."],
    deps = [
        ":trips",
        "@pip//boto3",
        "@pip//botocore",
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
            "backfill/**",
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

filegroup(
    name = "production_sources",
    srcs = glob(
        ["**/*.py"],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "tests/**/*.py",
            "backfill/**",
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
