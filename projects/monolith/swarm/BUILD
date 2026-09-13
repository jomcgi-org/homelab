load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")

# Bazel-native package ownership. Classification is declared here, not in
# projects/monolith/BUILD.
# Every Python test in this package has an explicit target below. Keep
# Gazelle from generating duplicate per-file py_test rules.
# gazelle:exclude *_test.py
# gazelle:exclude test_*.py

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")

package(default_visibility = ["//visibility:private"])

exports_files(
    glob(["**/*.py"]),
    visibility = ["//projects/monolith:private_backend_consumers"],
)

py_library(
    name = "swarm_rationale",
    srcs = ["rationale.py"],
    imports = [".."],
    visibility = ["//projects/monolith:private_backend_consumers"],
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
    visibility = ["//projects/monolith:private_backend_consumers"],
    deps = [
        "//projects/monolith/core",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_library(
    name = "swarm",
    srcs = glob(
        ["**/*.py"],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
            "__init__.py",
            "api.py",
            "factory_controls.py",
            "factory_models.py",
            "models.py",
            "rationale.py",
        ],
    ),
    imports = [".."],
    tags = ["monolith-backend"],
    visibility = ["//projects/monolith:private_backend_consumers"],
    deps = [
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//jsonschema",
        "@pip//opentelemetry_api",
        "@pip//pydantic",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "view_test",
    srcs = ["view_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "rows_test",
    srcs = ["rows_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "models_test",
    srcs = ["models_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_turn_artifact_test",
    srcs = ["turn_artifact_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_graph_test",
    srcs = ["graph_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_store_test",
    srcs = ["store_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "rationale_test",
    srcs = ["rationale_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        ":swarm_rationale",
        "@pip//pytest",
    ],
)

py_test(
    name = "deviations_test",
    srcs = ["deviations_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "budget_test",
    srcs = ["budget_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "tier_test",
    srcs = ["tier_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_config_test",
    srcs = ["config_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_runtime_test",
    srcs = ["runtime_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "@pip//dbos",
        "@pip//fastapi",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_policy_test",
    srcs = ["policy_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_classifier_test",
    srcs = ["classifier_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_factory_conductor_test",
    srcs = ["factory_conductor_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "//projects/monolith/agent_sessions",
        "//projects/monolith/auth",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_model_pool_test",
    srcs = ["model_pool_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "//projects/monolith/agent_sessions",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_factory_controls_test",
    srcs = ["factory_controls_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "//projects/monolith/auth",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_mcp_test",
    srcs = ["mcp_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "//projects/monolith/auth",
        "@pip//fastmcp",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "swarm_factory_intake_test",
    srcs = ["factory_intake_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "//projects/monolith/auth",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_factory_intake_loop_test",
    srcs = ["factory_intake_loop_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "//projects/monolith/auth",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_factory_landing_test",
    srcs = ["factory_landing_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "//projects/monolith/auth",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_factory_quota_guard_test",
    srcs = ["factory_quota_guard_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "//projects/monolith/auth",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_factory_refine_test",
    srcs = ["factory_refine_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "//projects/monolith/auth",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_factory_decisions_test",
    srcs = ["factory_decisions_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "//projects/monolith/auth",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_factory_escalation_test",
    srcs = ["factory_escalation_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "//projects/monolith/auth",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_factory_fence_test",
    srcs = ["factory_fence_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "//projects/monolith/auth",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_factory_router_test",
    srcs = ["factory_router_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "//projects/monolith/auth",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_factory_integration_test",
    srcs = ["factory_integration_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "//projects/monolith/auth",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_factory_supervision_test",
    srcs = ["factory_supervision_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "//projects/monolith/agent_sessions",
        "//projects/monolith/auth",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_factory_attempt_stop_test",
    srcs = ["factory_attempt_stop_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "//projects/monolith:pkg_cyclegroup",
        "//projects/monolith/agent_sessions",
        "//projects/monolith/auth",
        "//projects/monolith/core",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_node_workflows_test",
    srcs = ["node_workflows_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "//projects/monolith/agent_sessions",
        "//projects/monolith/auth",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_router_test",
    srcs = ["router_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_drainer_router_test",
    srcs = ["drainer_router_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "//projects/monolith:pkg_cyclegroup",
        "//projects/monolith/agent_sessions",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_health_test",
    srcs = ["health_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_drainer_test",
    srcs = ["drainer_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "//projects/monolith:pkg_cyclegroup",
        "//projects/monolith/agent_sessions",
        "//projects/monolith/knowledge",
        "@pip//opentelemetry_sdk",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_drain_console_test",
    srcs = ["drain_console_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_drain_console_router_test",
    srcs = ["drain_console_router_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_compare_router_test",
    srcs = ["compare_router_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_unified_diff_test",
    srcs = ["unified_diff_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_walkthrough_composer_test",
    srcs = ["walkthrough_composer_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_workflows_test",
    srcs = ["workflows_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_steps_test",
    srcs = ["steps_test.py"],
    imports = [".."],
    deps = [
        ":swarm",
        "@pip//httpx",
        "@pip//pytest",
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
    name = "api_semgrep_test",
    srcs = ["api.py"],
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
    name = "budget_semgrep_test",
    srcs = ["budget.py"],
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
    name = "classifier_semgrep_test",
    srcs = ["classifier.py"],
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
    name = "compare_router_semgrep_test",
    srcs = ["compare_router.py"],
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
    name = "config_semgrep_test",
    srcs = ["config.py"],
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
    name = "deviations_semgrep_test",
    srcs = ["deviations.py"],
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
    name = "drain_console_semgrep_test",
    srcs = ["drain_console.py"],
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
    name = "drain_console_router_semgrep_test",
    srcs = ["drain_console_router.py"],
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
    name = "drainer_semgrep_test",
    srcs = ["drainer.py"],
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
    name = "drainer_router_semgrep_test",
    srcs = ["drainer_router.py"],
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
    name = "factory_attempt_stop_semgrep_test",
    srcs = ["factory_attempt_stop.py"],
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
    name = "factory_conductor_semgrep_test",
    srcs = ["factory_conductor.py"],
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
    name = "factory_controls_semgrep_test",
    srcs = ["factory_controls.py"],
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
    name = "factory_decisions_semgrep_test",
    srcs = ["factory_decisions.py"],
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
    name = "factory_intake_semgrep_test",
    srcs = ["factory_intake.py"],
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
    name = "factory_intake_loop_semgrep_test",
    srcs = ["factory_intake_loop.py"],
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
    name = "factory_landing_semgrep_test",
    srcs = ["factory_landing.py"],
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
    name = "factory_models_semgrep_test",
    srcs = ["factory_models.py"],
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
    name = "factory_quota_guard_semgrep_test",
    srcs = ["factory_quota_guard.py"],
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
    name = "factory_refine_semgrep_test",
    srcs = ["factory_refine.py"],
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
    name = "factory_router_semgrep_test",
    srcs = ["factory_router.py"],
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
    name = "factory_supervision_semgrep_test",
    srcs = ["factory_supervision.py"],
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
    name = "graph_semgrep_test",
    srcs = ["graph.py"],
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
    name = "mcp_semgrep_test",
    srcs = ["mcp.py"],
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
    name = "model_pool_semgrep_test",
    srcs = ["model_pool.py"],
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
    name = "node_workflows_semgrep_test",
    srcs = ["node_workflows.py"],
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
    name = "policy_semgrep_test",
    srcs = ["policy.py"],
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
    name = "queues_semgrep_test",
    srcs = ["queues.py"],
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
    name = "rationale_semgrep_test",
    srcs = ["rationale.py"],
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
    name = "rows_semgrep_test",
    srcs = ["rows.py"],
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
    name = "runtime_semgrep_test",
    srcs = ["runtime.py"],
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
    name = "steps_semgrep_test",
    srcs = ["steps.py"],
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
    name = "store_semgrep_test",
    srcs = ["store.py"],
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
    name = "tracing_semgrep_test",
    srcs = ["tracing.py"],
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
    name = "turn_artifact_semgrep_test",
    srcs = ["turn_artifact.py"],
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
    name = "unified_diff_semgrep_test",
    srcs = ["unified_diff.py"],
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
    name = "view_semgrep_test",
    srcs = ["view.py"],
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
    name = "walkthrough_composer_semgrep_test",
    srcs = ["walkthrough_composer.py"],
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
    name = "workflows_semgrep_test",
    srcs = ["workflows.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)
