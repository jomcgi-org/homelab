# Bazel-native package ownership. Classification is declared here, not in
# projects/monolith/BUILD.
# The root monolith Semgrep target already scans this package transitively.
# gazelle:semgrep disabled
# Every Python test in this package has an explicit target below. Keep
# Gazelle from generating duplicate per-file py_test rules.
# gazelle:exclude *_test.py
# gazelle:exclude test_*.py
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
    name = "hikes",
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
        "//projects/monolith/scheduler",
        "//projects/monolith/shared",
        "@pip//beautifulsoup4",
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
        "@pip//timelength",
        "@pip//tzdata",
        "@pip//uvicorn",
    ],
)

py_test(
    name = "hikes_models_test",
    srcs = ["models_test.py"],
    imports = [".."],
    deps = [
        ":hikes",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "hikes_walkhighlands_test",
    srcs = ["walkhighlands_test.py"],
    imports = [".."],
    deps = [
        ":hikes",
        "@pip//pytest",
    ],
)

py_test(
    name = "hikes_forecast_test",
    srcs = ["forecast_test.py"],
    imports = [".."],
    deps = [
        ":hikes",
        "@pip//pytest",
    ],
)

py_test(
    name = "hikes_router_test",
    srcs = ["router_test.py"],
    imports = [".."],
    deps = [
        ":hikes",
        "//projects/monolith/core",
        "//projects/monolith/shared",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "hikes_jobs_test",
    srcs = ["jobs_test.py"],
    imports = [".."],
    deps = [
        ":hikes",
        "//projects/monolith/shared",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "hikes_init_test",
    srcs = ["__init___test.py"],
    imports = [".."],
    deps = [
        ":hikes",
        "@pip//pytest",
    ],
)

py_test(
    name = "hikes_generate_seed_test",
    srcs = [
        "tools/generate_seed.py",
        "tools/generate_seed_test.py",
    ],
    imports = ["tools"],
    deps = ["@pip//pytest"],
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
