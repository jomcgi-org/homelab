load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")

# Bazel-native package ownership. Classification is declared here, not in
# projects/monolith/BUILD.
# Every Python test in this package has an explicit target below. Keep
# Gazelle from generating duplicate per-file py_test rules.
# gazelle:exclude *_test.py
# gazelle:exclude test_*.py
# Nested sources remain owned by this package's recursive targets.
# gazelle:exclude grid_gen

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

package(default_visibility = ["//visibility:private"])

exports_files(
    glob(["**/*.py"]),
    visibility = ["//projects/monolith:private_backend_consumers"],
)

py_library(
    name = "stars",
    srcs = glob(
        [
            "**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
            "grid_gen/**",
        ],
    ),
    imports = [".."],
    tags = ["monolith-backend"],
    visibility = ["//projects/monolith:private_backend_consumers"],
    deps = [
        "@pip//astral",
        "@pip//boto3",
        "@pip//botocore",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "stars_models_test",
    srcs = ["models_test.py"],
    imports = [".."],
    deps = [
        ":stars",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "stars_scoring_test",
    srcs = ["scoring_test.py"],
    imports = [".."],
    deps = [
        ":stars",
        "@pip//pydantic",
        "@pip//pytest",
    ],
)

py_test(
    name = "stars_forecast_test",
    srcs = ["forecast_test.py"],
    imports = [".."],
    deps = [
        ":stars",
        "@pip//pytest",
    ],
)

py_test(
    name = "stars_jobs_test",
    srcs = ["jobs_test.py"],
    imports = [".."],
    deps = [
        ":stars",
        "//projects/monolith/shared",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "stars_grid_test",
    srcs = ["grid_test.py"],
    imports = [".."],
    deps = [
        ":stars",
        "//projects/monolith/core",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "stars_climatology_test",
    srcs = ["climatology_test.py"],
    imports = [".."],
    deps = [
        ":stars",
        "//projects/monolith/core",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "stars_router_test",
    srcs = ["router_test.py"],
    imports = [".."],
    deps = [
        ":stars",
        "//projects/monolith/core",
        "//projects/monolith/shared",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "stars_health_test",
    srcs = ["health_test.py"],
    imports = [".."],
    deps = [
        ":stars",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "stars_init_test",
    srcs = ["__init___test.py"],
    imports = [".."],
    deps = [
        ":stars",
        "@pip//pytest",
    ],
)

py_test(
    name = "stars_generate_grid_test",
    srcs = [
        "grid_gen/generate_grid.py",
        "grid_gen/generate_grid_test.py",
    ],
    imports = ["grid_gen"],
    deps = ["@pip//pytest"],
)

py_test(
    name = "stars_generate_grid_v2_test",
    srcs = [
        "grid_gen/generate_grid_v2.py",
        "grid_gen/generate_grid_v2_test.py",
    ],
    imports = ["grid_gen"],
    deps = ["@pip//pytest"],
)

py_test(
    name = "stars_backfill_cerra_test",
    srcs = [
        "grid_gen/backfill_cerra.py",
        "grid_gen/backfill_cerra_test.py",
    ],
    imports = ["grid_gen"],
    deps = [
        "@pip//numpy",
        "@pip//pytest",
    ],
)

py_test(
    name = "stars_backfill_climatology_test",
    srcs = [
        "grid_gen/backfill_climatology.py",
        "grid_gen/backfill_climatology_test.py",
    ],
    imports = ["grid_gen"],
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
            "grid_gen/**",
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
            "grid_gen/**",
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
    name = "forecast_semgrep_test",
    srcs = ["forecast.py"],
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
    name = "grid_semgrep_test",
    srcs = ["grid.py"],
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
    name = "health_semgrep_test",
    srcs = ["health.py"],
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
    name = "jobs_semgrep_test",
    srcs = ["jobs.py"],
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
    name = "materialize_semgrep_test",
    srcs = ["materialize.py"],
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
    name = "scoring_semgrep_test",
    srcs = ["scoring.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)
