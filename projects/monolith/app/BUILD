# Bazel-native package ownership. Classification is declared here, not in
# projects/monolith/BUILD.
# The root monolith Semgrep target already scans this package transitively.
# gazelle:semgrep disabled
# Every Python test in this package has an explicit target below. Keep
# Gazelle from generating duplicate per-file py_test rules.
# gazelle:exclude *_test.py
# gazelle:exclude test_*.py
# Root deployable targets already own these entrypoints.
# gazelle:exclude agents_main.py
# gazelle:exclude jobs_main.py
# gazelle:exclude main.py
# gazelle:exclude main_domain.py
# gazelle:exclude main_public.py
# gazelle:exclude progress_main.py

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

package(default_visibility = ["//visibility:private"])

exports_files(
    glob(["**/*.py"]),
    visibility = ["//projects/monolith:private_backend_consumers"],
)

# Recursive sources and reviewed runtime dependencies are package-owned.
# keep
py_library(
    name = "app",
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
)

py_test(
    name = "frontend_test",
    srcs = ["frontend_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
        "@pip//tzdata",
    ],
)

py_test(
    name = "jobs_main_test",
    srcs = ["jobs_main_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:monolith_backend",
        "@pip//pytest",
        "@pip//sqlmodel",
        "@pip//typer",
    ],
)

py_test(
    name = "main_test",
    srcs = ["main_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
        "@pip//tzdata",
    ],
)

py_test(
    name = "main_public_routes_test",
    srcs = ["main_public_routes_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:monolith_backend",
        "@pip//fastapi",
        "@pip//pytest",
        "@pip//sqlmodel",
        "@pip//tzdata",
    ],
)

py_test(
    name = "main_public_imports_test",
    srcs = ["main_public_imports_test.py"],
    imports = [".."],
    # Depend on the PRUNED public library, not :monolith_backend. This makes the
    # test a pre-deploy validator of the prune itself: if the public glob omits a
    # file that app.main_public actually needs, the child import fails
    # (returncode != 0) here in CI rather than only at pod startup. It still
    # asserts the forbidden private modules are absent from the closure.
    deps = [
        "//projects/monolith:monolith_public_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "main_agents_imports_test",
    srcs = ["main_agents_imports_test.py"],
    imports = [".."],
    # Depend on the PRUNED agents library, not :monolith_backend. This makes
    # the test validate both the source prune and the fresh-process import
    # closure before any later image is assembled.
    deps = [
        "//projects/monolith:monolith_agents_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "main_domain_test",
    srcs = ["main_domain_test.py"],
    data = ["//projects/monolith:domain_images_test_data"],
    imports = [".."],
    deps = [
        "//projects/monolith:monolith_backend",
        "@pip//fastapi",
        "@pip//pytest",
        "@pip//sqlmodel",
        "@pip//tzdata",
    ],
)

py_test(
    name = "integration_test",
    size = "medium",
    srcs = ["integration_test.py"],
    imports = [".."],
    tags = ["integration"],
    deps = [
        "//projects/monolith:monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
        "@pip//tzdata",
    ],
)

py_test(
    name = "app_architecture_test",
    srcs = ["architecture_test.py"],
    data = [
        "//projects/monolith/chat:production_sources",
        "//projects/monolith/home:production_sources",
        "//projects/monolith/knowledge:production_sources",
    ],
    imports = [".."],
    deps = [
        "//projects/monolith:monolith_backend",
        "@pip//fastapi",
        "@pip//pytest",
    ],
)

py_test(
    name = "main_lifespan_discord_test",
    srcs = ["main_lifespan_discord_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:monolith_backend",
        "@pip//discord_py",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
        "@pip//tzdata",
    ],
)

py_test(
    name = "main_otel_test",
    srcs = ["main_otel_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
        "@pip//tzdata",
    ],
)

py_test(
    name = "main_extra_test",
    srcs = ["main_extra_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:monolith_backend",
        "@pip//discord_py",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
        "@pip//tzdata",
    ],
)

py_test(
    name = "main_log_task_test",
    srcs = ["main_log_task_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:monolith_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "main_sidecar_test",
    srcs = ["main_sidecar_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:monolith_backend",
        "@pip//discord_py",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
        "@pip//tzdata",
    ],
)

py_test(
    name = "app_main_summary_test",
    srcs = ["main_summary_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
        "@pip//tzdata",
    ],
)

py_test(
    name = "app_main_summary_edge_test",
    srcs = ["main_summary_edge_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
        "@pip//tzdata",
    ],
)

py_test(
    name = "main_lock_sweep_test",
    srcs = ["main_lock_sweep_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:monolith_backend",
        "@pip//discord_py",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
        "@pip//tzdata",
    ],
)

py_test(
    name = "main_app_state_test",
    srcs = ["main_app_state_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:monolith_backend",
        "@pip//discord_py",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
        "@pip//tzdata",
    ],
)

py_test(
    name = "main_log_coverage_test",
    srcs = ["main_log_coverage_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
        "@pip//tzdata",
    ],
)

py_test(
    name = "bdd_completeness_test",
    size = "small",
    srcs = [
        "__init__.py",
        "bdd_completeness_test.py",
    ],
    # The data glob hoovers up EVERY domain's tests, so each one needs an
    # explicit --ignore below or pytest collects and runs it here. A `future`
    # tag does not help: that tags the dedicated bdd_test target, while this is
    # a separate py_test that picks the file up by glob.
    data = [
        "//projects/monolith/agent:tests/bdd_checks_test.py",
        "//projects/monolith/agent:tests/bdd_drainer_test.py",
        "//projects/monolith/agent:tests/bdd_locks_test.py",
        "//projects/monolith/agent:tests/bdd_routine_jobs_test.py",
        "//projects/monolith/chat:tests/bdd_api_test.py",
        "//projects/monolith/goosecracker:tests/recipe_catalog_test.py",
        "//projects/monolith/goosecracker:tests/repo_catalog_test.py",
        "//projects/monolith/home:tests/bdd_api_test.py",
        "//projects/monolith/home:tests/bdd_playwright_test.py",
        "//projects/monolith/home:tests/bdd_public_test.py",
        "//projects/monolith/knowledge:tests/bdd_api_test.py",
        "//projects/monolith/knowledge:tests/bdd_health_kg_test.py",
        "//projects/monolith/knowledge:tests/bdd_playwright_test.py",
        "//projects/monolith/knowledge:tests/bdd_public_test.py",
        "//projects/monolith/knowledge:tests/bdd_raws_test.py",
        "//projects/monolith/knowledge:tests/bdd_raws_usage_test.py",
        "//projects/monolith/knowledge:tests/bdd_reports_test.py",
        "//projects/monolith/moving:tests/chat_test.py",
        "//projects/monolith/moving:tests/collisions_test.py",
        "//projects/monolith/moving:tests/gcal_sync_test.py",
        "//projects/monolith/moving:tests/models_test.py",
        "//projects/monolith/moving:tests/router_test.py",
        "//projects/monolith/moving:tests/viewer_test.py",
        "//projects/monolith/shotter:tests/bdd_mcp_tool_test.py",
    ],
    env = {"PYTEST_ADDOPTS": "--import-mode=importlib --ignore=projects/monolith/home/tests --ignore=projects/monolith/chat/tests --ignore=projects/monolith/knowledge/tests --ignore=projects/monolith/scheduler/tests --ignore=projects/monolith/agent/tests --ignore=projects/monolith/goosecracker/tests --ignore=projects/monolith/moving/tests --ignore=projects/monolith/shotter/tests"},
    imports = [".."],
    tags = ["bdd"],
    deps = [
        "//projects/monolith:monolith_backend",
        "//projects/monolith/shared:shared_testing",
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
            "main.py",
            "mcp_app.py",
            "modules_private.py",
            "main_domain.py",
            "jobs_main.py",
        ],
    ),
    imports = [".."],
    tags = ["monolith-public"],
    visibility = ["//projects/monolith/public_backend:__pkg__"],
)

# keep
py_library(
    name = "agents",
    srcs = glob(
        [
            "**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "tests/**/*.py",
            "main.py",
            "mcp_app.py",
            "modules_private.py",
            "main_domain.py",
            "jobs_main.py",
            "progress_main.py",
            "main_agents_imports_test.py",
        ],
    ),
    imports = [".."],
    tags = ["monolith-agents"],
    visibility = ["//projects/monolith:__pkg__"],
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
