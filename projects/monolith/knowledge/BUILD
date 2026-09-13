load("//bazel/semgrep/defs:defs.bzl", "semgrep_test")

# Bazel-native package ownership. Classification is declared here, not in
# projects/monolith/BUILD.
# Every Python test in this package has an explicit target below. Keep
# Gazelle from generating duplicate per-file py_test rules.
# gazelle:exclude *_test.py
# gazelle:exclude test_*.py
# The explicit migrate_raw_bucketing target owns this executable entrypoint.
# gazelle:exclude migrate_raw_bucketing.py
# Nested sources remain owned by this package's recursive targets.
# gazelle:exclude tests
# gazelle:exclude tools
# These suites use the BDD harness and are owned by explicit bdd_test targets.
# gazelle:exclude tests/bdd_api_test.py
# gazelle:exclude tests/bdd_health_kg_test.py
# gazelle:exclude tests/bdd_playwright_test.py
# gazelle:exclude tests/bdd_public_test.py
# gazelle:exclude tests/bdd_raws_test.py
# gazelle:exclude tests/bdd_raws_usage_test.py
# gazelle:exclude tests/bdd_reports_test.py

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")
load("//projects/monolith:bdd_test.bzl", "bdd_test")

package(default_visibility = ["//visibility:private"])

exports_files(
    glob(["**/*.py"]),
    visibility = ["//projects/monolith:private_backend_consumers"],
)

py_library(
    name = "knowledge",
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
    # entities.yaml is 2.5 KB and deliberately ships as runfiles data, unlike
    # the multi-megabyte ndjson manifest isolated in its own image layer below.
    data = ["entities.yaml"],
    imports = [".."],
    tags = ["monolith-backend"],
    visibility = ["//projects/monolith:private_backend_consumers"],
    deps = [
        "@pip//boto3",
        "@pip//botocore",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//networkx",
        "@pip//pydantic",
        "@pip//pyyaml",
        "@pip//sqlmodel",
        "@pip//trafilatura",
    ],
)

py_test(
    name = "knowledge_extraction_test",
    srcs = ["extraction_test.py"],
    data = ["testdata/codex_session_redacted.txt"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pydantic",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_entities_test",
    srcs = ["entities_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_extraction_gates_test",
    srcs = ["extraction_gates_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_recall_test",
    srcs = ["recall_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "//projects/monolith/agent_sessions",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_gate_calibration_test",
    srcs = ["gate_calibration_test.py"],
    data = [
        "testdata/atoms-all.tsv",
        "testdata/atoms-graded.tsv",
    ],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_lens_test",
    srcs = ["lens_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_docfix_test",
    srcs = ["docfix_test.py"],
    data = [
        "//projects/monolith/chart:docfix_review_test_data",
        "//projects/monolith/deploy:deploy_values",
        "@multitool//tools/helm",
    ],
    env = {
        "DEPLOY_VALUES": "$(rootpath //projects/monolith/deploy:deploy_values)",
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
    },
    imports = [".."],
    deps = [
        ":knowledge",
        "//projects/monolith/swarm",
        "@pip//opentelemetry_sdk",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_extraction_repo_diff_test",
    srcs = ["extraction_repo_diff_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_health_test",
    srcs = ["health_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_burst_test",
    srcs = ["burst_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_chunker_test",
    srcs = ["chunker_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_chunker_extra_test",
    srcs = ["chunker_extra_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_chunker_split_edge_cases_test",
    srcs = ["chunker_split_edge_cases_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_frontmatter_test",
    srcs = ["frontmatter_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

py_test(
    name = "knowledge_frontmatter_scoped_test",
    srcs = ["frontmatter_scoped_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_layout_test",
    srcs = ["layout_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//networkx",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_links_test",
    srcs = ["links_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_links_extra_test",
    srcs = ["links_extra_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_links_coverage_test",
    srcs = ["links_coverage_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_store_test",
    srcs = ["store_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pgvector",
        "@pip//pydantic",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_store_scoped_test",
    srcs = ["store_scoped_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_indexing_test",
    srcs = ["indexing_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "knowledge_wikilinks_test",
    srcs = ["wikilinks_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_visibility_test",
    srcs = ["visibility_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_profile_test",
    srcs = ["profile_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_tools_add_visibility_field_test",
    srcs = ["tools/add_visibility_field_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_tools_classify_visibility_backfill_test",
    srcs = ["tools/classify_visibility_backfill_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_gen_repo_docs_manifest_test",
    srcs = ["gen_repo_docs_manifest_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_gen_docs_manifest_test",
    srcs = ["gen_docs_manifest_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_gen_posts_manifest_test",
    srcs = ["gen_posts_manifest_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_tools_prettier_json_test",
    srcs = ["tools/prettier_json_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_gardener_test",
    srcs = ["gardener_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_models_test",
    srcs = ["models_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_gap_model_test",
    srcs = ["gap_model_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_gap_set_class_test",
    srcs = ["gap_set_class_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_gap_answer_fileless_test",
    srcs = ["gap_answer_fileless_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_store_gap_queries_test",
    srcs = ["store_gap_queries_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_service_test",
    srcs = ["service_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "//projects/monolith/scheduler",
        "@pip//networkx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_store_extra_test",
    srcs = ["store_extra_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_frontmatter_extra_test",
    srcs = ["frontmatter_extra_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

py_test(
    name = "knowledge_frontmatter_json_safe_test",
    srcs = ["frontmatter_json_safe_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

py_test(
    name = "knowledge_store_zero_chunk_test",
    srcs = ["store_zero_chunk_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_store_optional_metadata_test",
    srcs = ["store_optional_metadata_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_raw_paths_test",
    srcs = ["raw_paths_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_migrate_raw_bucketing_test",
    srcs = ["migrate_raw_bucketing_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_fixes_coverage_test",
    srcs = ["knowledge_fixes_coverage_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//pyyaml",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_router_test",
    srcs = ["router_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "//projects/monolith/core",
        "//projects/monolith/framework",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_router_raws_test",
    srcs = ["router_raws_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "//projects/monolith/core",
        "//projects/monolith/framework",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_redact_test",
    srcs = ["redact_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_publish_test",
    srcs = ["publish_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_notes_crud_test",
    srcs = ["notes_crud_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "//projects/monolith/core",
        "//projects/monolith/framework",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pyyaml",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
        "@pip//tzdata",
    ],
)

py_test(
    name = "knowledge_mcp_test",
    srcs = ["mcp_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//fastmcp",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "knowledge_mcp_registry_test",
    srcs = ["mcp_registry_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//fastmcp",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "knowledge_mcp_reports_test",
    srcs = ["mcp_reports_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "//projects/monolith:pkg_cyclegroup",
        "//projects/monolith/auth",
        "@pip//fastmcp",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_interventions_test",
    srcs = ["interventions_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "//projects/monolith:pkg_cyclegroup",
        "//projects/monolith/auth",
        "//projects/monolith/core",
        "//projects/monolith/swarm:swarm_factory_controls",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_gap_api_test",
    srcs = ["gap_api_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "//projects/monolith/core",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pyyaml",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_gap_review_endpoints_test",
    srcs = ["gap_review_endpoints_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "//projects/monolith/core",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_note_review_endpoints_test",
    srcs = ["note_review_endpoints_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "//projects/monolith/core",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pyyaml",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_notes_test",
    srcs = ["notes_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pyyaml",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "tasks_router_test",
    srcs = ["tasks_router_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "//projects/monolith/core",
        "//projects/monolith/framework",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_raw_store_test",
    srcs = ["raw_store_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//boto3",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_store_note_links_test",
    srcs = ["store_note_links_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_ingest_queue_claim_test",
    srcs = ["ingest_queue_claim_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_ingest_queue_webpage_title_test",
    srcs = ["ingest_queue_webpage_title_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "knowledge_ingest_queue_router_test",
    srcs = ["ingest_queue_router_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "//projects/monolith/core",
        "//projects/monolith/framework",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_dead_letter_test",
    srcs = ["dead_letter_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "//projects/monolith/core",
        "//projects/monolith/framework",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "tasks_store_test",
    srcs = ["tasks_store_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "tasks_rollup_test",
    srcs = ["tasks_rollup_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

bdd_test(
    name = "knowledge_bdd_api_test",
    srcs = [
        "tests/__init__.py",
        "tests/bdd_api_test.py",
        "tests/bdd_health_kg_test.py",
        "tests/bdd_raws_test.py",
        "tests/bdd_raws_usage_test.py",
        "tests/bdd_reports_test.py",
        "tests/conftest.py",
    ],
)

bdd_test(
    name = "knowledge_bdd_playwright_test",
    timeout = "long",
    srcs = [
        "tests/__init__.py",
        "tests/bdd_playwright_test.py",
        "tests/conftest.py",
    ],
    playwright = True,
)

bdd_test(
    name = "knowledge_bdd_public_test",
    srcs = [
        "tests/__init__.py",
        "tests/bdd_public_test.py",
        "tests/conftest.py",
    ],
)

py_test(
    name = "knowledge_http_cache_test",
    srcs = ["http_cache_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_public_models_test",
    srcs = ["public_models_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_public_router_test",
    srcs = ["public_router_test.py"],
    imports = [".."],
    deps = [
        ":knowledge",
        "//projects/monolith/core",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
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
            "publish.py",
            "router.py",
            "tasks_router.py",
            "gaps.py",
            "ingest_queue.py",
            "mcp.py",
            "service.py",
            "layout.py",
        ],
    ),
    data = ["entities.yaml"],
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
            "publish.py",
            "router.py",
            "tasks_router.py",
            "gaps.py",
            "ingest_queue.py",
            "service.py",
            "layout.py",
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

filegroup(
    name = "repo_docs_manifest",
    srcs = ["repo_docs_manifest.ndjson"],
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
    name = "atoms_semgrep_test",
    srcs = ["atoms.py"],
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
    name = "burst_semgrep_test",
    srcs = ["burst.py"],
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
    name = "chunker_semgrep_test",
    srcs = ["chunker.py"],
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
    name = "docfix_semgrep_test",
    srcs = ["docfix.py"],
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
    name = "entities_semgrep_test",
    srcs = ["entities.py"],
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
    name = "extraction_semgrep_test",
    srcs = ["extraction.py"],
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
    name = "frontmatter_semgrep_test",
    srcs = ["frontmatter.py"],
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
    name = "gaps_semgrep_test",
    srcs = ["gaps.py"],
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
    name = "gardener_semgrep_test",
    srcs = ["gardener.py"],
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
    name = "http_cache_semgrep_test",
    srcs = ["http_cache.py"],
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
    name = "indexing_semgrep_test",
    srcs = ["indexing.py"],
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
    name = "ingest_queue_semgrep_test",
    srcs = ["ingest_queue.py"],
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
    name = "interventions_semgrep_test",
    srcs = ["interventions.py"],
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
    name = "layout_semgrep_test",
    srcs = ["layout.py"],
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
    name = "links_semgrep_test",
    srcs = ["links.py"],
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
    name = "notes_semgrep_test",
    srcs = ["notes.py"],
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
    name = "profile_semgrep_test",
    srcs = ["profile.py"],
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
    name = "public_limits_semgrep_test",
    srcs = ["public_limits.py"],
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
    name = "public_models_semgrep_test",
    srcs = ["public_models.py"],
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
    name = "public_router_semgrep_test",
    srcs = ["public_router.py"],
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
    name = "publish_semgrep_test",
    srcs = ["publish.py"],
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
    name = "raw_paths_semgrep_test",
    srcs = ["raw_paths.py"],
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
    name = "raw_store_semgrep_test",
    srcs = ["raw_store.py"],
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
    name = "recall_semgrep_test",
    srcs = ["recall.py"],
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
    name = "redact_semgrep_test",
    srcs = ["redact.py"],
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
    name = "repo_docs_semgrep_test",
    srcs = ["repo_docs.py"],
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
    name = "service_semgrep_test",
    srcs = ["service.py"],
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
    name = "tasks_router_semgrep_test",
    srcs = ["tasks_router.py"],
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
    name = "visibility_semgrep_test",
    srcs = ["visibility.py"],
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
    name = "wikilinks_semgrep_test",
    srcs = ["wikilinks.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)
