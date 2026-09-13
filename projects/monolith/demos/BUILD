# Bazel-native package ownership. Classification is declared here, not in
# projects/monolith/BUILD.
# The root monolith Semgrep target already scans this package transitively.
# gazelle:semgrep disabled
# Every Python test in this package has an explicit target below. Keep
# Gazelle from generating duplicate per-file py_test rules.
# gazelle:exclude *_test.py
# gazelle:exclude test_*.py
# Nested sources remain owned by this package's recursive targets.
# gazelle:exclude loadtest_corpus

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

package(default_visibility = ["//visibility:private"])

exports_files(
    glob(["**/*.py"]),
    visibility = ["//projects/monolith:private_backend_consumers"],
)

# keep: recursive sources and reviewed runtime dependencies are package-owned.
py_library(
    name = "demos",
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
        "//projects/monolith:pkg_cyclegroup",
        "//projects/monolith/cluster",
        "//projects/monolith/core",
        "//projects/monolith/ember_public",
        "//projects/monolith/framework",
        "//projects/monolith/sandbox",
        "//projects/monolith/semgrep_scan",
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
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
        "@pip//tzdata",
        "@pip//uvicorn",
    ],
)

py_test(
    name = "demos_firecracker_api_test",
    srcs = ["firecracker_api_test.py"],
    imports = [".."],
    deps = [
        ":demos",
        "//projects/monolith/ember_public",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "demos_loadtest_corpus_test",
    srcs = ["loadtest_corpus_test.py"],
    data = glob(["loadtest_corpus/**/*.sample"]),
    imports = [".."],
    deps = [
        ":demos",
        "@pip//pytest",
    ],
)

py_test(
    name = "demos_loadtest_test",
    srcs = ["loadtest_test.py"],
    # run_load_test defaults its corpus to load_corpus(workload), which reads
    # the committed .sample files at runtime.
    data = glob(["loadtest_corpus/**/*.sample"]),
    imports = [".."],
    deps = [
        ":demos",
        "@pip//pytest",
        "@pip//pytest_asyncio",
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

filegroup(
    name = "loadtest_samples",
    srcs = glob(["loadtest_corpus/**/*.sample"]),
    visibility = ["//projects/monolith:__pkg__"],
)
