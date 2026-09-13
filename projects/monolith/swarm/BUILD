# gazelle:ignore

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
    name = "swarm_rationale",
    srcs = ["rationale.py"],
    imports = [".."],
    tags = ["monolith-private"],
    visibility = ["//projects/monolith:package_consumers"],
    deps = ["//projects/monolith/agent_sessions:agent_sessions_rationale"],
)

py_library(
    name = "swarm_factory_controls",
    srcs = [
        "__init__.py",
        "api.py",
        "factory_controls.py",
        "factory_models.py",
        "models.py",
    ],
    imports = [".."],
    tags = ["monolith-private"],
    visibility = ["//projects/monolith:package_consumers"],
    deps = [
        "//projects/monolith/core:pkg_core",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_library(
    name = "pkg_swarm",
    srcs = glob(
        ["**/*.py"],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "tests/**/*.py",
            "__init__.py",
            "api.py",
            "factory_controls.py",
            "factory_models.py",
            "models.py",
            "rationale.py",
        ],
    ),
    imports = [".."],
    tags = ["monolith-private"],
    visibility = ["//projects/monolith:package_consumers"],
    deps = [
        "//projects/monolith/agent:pkg_cyclegroup",
        "//projects/monolith/agent_sessions:agent_sessions_admission",
        "//projects/monolith/agent_sessions:agent_sessions_rationale",
        "//projects/monolith/agent_sessions:pkg_agent_sessions",
        "//projects/monolith/core:pkg_core",
        "//projects/monolith/framework:pkg_framework",
        "//projects/monolith/knowledge:pkg_knowledge",
        "//projects/monolith/swarm:swarm_factory_controls",
        "@pip//certifi",
        "@pip//dbos",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//jsonschema",
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
