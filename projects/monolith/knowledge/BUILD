# Bazel-native package ownership. Classification is declared here, not in
# projects/monolith/BUILD.

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
        "//projects/monolith/core",
        "//projects/monolith/framework",
        "//projects/monolith/scheduler",
        "//projects/monolith/shared",
        "//projects/monolith/swarm:swarm_factory_controls",
        "@pip//boto3",
        "@pip//certifi",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//lxml_html_clean",
        "@pip//networkx",
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
        "@pip//trafilatura",
        "@pip//tzdata",
        "@pip//uvicorn",
        "@pip//youtube_transcript_api",
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
