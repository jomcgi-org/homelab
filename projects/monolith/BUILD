# gazelle:exclude agent
# gazelle:exclude chat
# gazelle:exclude app
# gazelle:exclude e2e
# gazelle:exclude knowledge
# gazelle:exclude notes
# gazelle:exclude scripts
# gazelle:exclude home
# gazelle:exclude scheduler
# gazelle:exclude shared
# gazelle:semgrep_exclude_rules avoid-sqlalchemy-text,sqlmodel-datetime-without-factory,tainted-fastapi-http-request-httpx,tainted-path-traversal-stdlib-fastapi
load("@aspect_bazel_lib//lib:tar.bzl", "tar")
load("@aspect_rules_py//py:defs.bzl", "py_library")
load("@aspect_rules_py//py/private/py_venv:defs.bzl", "py_venv_binary")
load("@rules_shell//shell:sh_binary.bzl", "sh_binary")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_target_test")
load("//bazel/tools/js:exec_filegroup.bzl", "exec_filegroup")
load("//bazel/tools/oci:py3_image.bzl", "py3_image")
load("//bazel/tools/pytest:defs.bzl", "py_test")
load("//projects/monolith:bdd_test.bzl", "bdd_test")

# Pin the frontend build to the exec (host) platform. The vite output is
# just static HTML/CSS/JS — it's arch-independent — but py3_image's
# platform transitions would otherwise cause aspect_rules_js to select
# wrong-arch native binaries (esbuild, rollup) for the arm64 variant.
exec_filegroup(
    name = "frontend_dist",
    src = "//projects/monolith/frontend:build",
)

py_venv_binary(
    name = "main",
    srcs = glob(
        [
            "agent/**/*.py",
            "app/**/*.py",
            "chat/**/*.py",
            "knowledge/**/*.py",
            "home/**/*.py",
            "scheduler/**/*.py",
            "shared/**/*.py",
        ],
        exclude = [
            # pytest discovers both *_test.py (sibling convention) and
            # test_*.py (its default). Exclude both so that any test
            # file in these source trees stays out of the production
            # binary AND out of downstream test targets' runfiles.
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
            "shared/testing/**/*.py",
        ],
    ),  # keep
    data = [":frontend_dist"],
    imports = ["."],  # keep
    main = "app/main.py",
    visibility = ["//:__subpackages__"],
    deps = [":monolith_backend"],  # keep
)

# Phase-1 vault-mutation script: walk a vault and inject `visibility:` lines
# into note frontmatter that lacks the key. Run via `bazel run`:
#   bazel run //projects/monolith:add_visibility_field -- \
#       --vault-root /path/to/vault --dirs _processed
py_venv_binary(
    name = "add_visibility_field",
    main = "knowledge/tools/add_visibility_field.py",
    visibility = ["//:__subpackages__"],
)

# Phase-2 vault-mutation script: classify every unlabelled note via the
# `claude` CLI subprocess and write the decision back into frontmatter.
# Run via `bazel run`:
#   bazel run //projects/monolith:classify_visibility_backfill -- \
#       --vault-root /path/to/vault --report /tmp/visibility-report.json
# The report path MUST be outside the repo working tree — rationales
# quote note content and must never be committed.
py_venv_binary(
    name = "classify_visibility_backfill",
    main = "knowledge/tools/classify_visibility_backfill.py",
    visibility = ["//:__subpackages__"],
)

py_library(
    name = "monolith_backend",
    srcs = glob(
        [
            "agent/**/*.py",
            "app/**/*.py",
            "chat/**/*.py",
            "knowledge/**/*.py",
            "home/**/*.py",
            "scheduler/**/*.py",
            "shared/**/*.py",
        ],
        exclude = [
            # pytest discovers both *_test.py (sibling convention) and
            # test_*.py (its default). Exclude both so a broken test in
            # one knowledge/*.py file does not cascade across every
            # downstream test target that depends on this library
            # (per the 217-target cascade on PR #2349).
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
            "shared/testing/**/*.py",
        ],
    ),  # keep
    visibility = ["//:__subpackages__"],
    deps = [
        "@pip//discord_py",
        "@pip//dulwich",
        "@pip//fastapi",
        "@pip//fastmcp",
        "@pip//httpx",
        "@pip//icalendar",
        "@pip//kubernetes_asyncio",
        "@pip//lxml_html_clean",  # explicit: overrides.txt strips lxml[html-clean] extra
        "@pip//networkx",
        "@pip//opentelemetry_exporter_otlp_proto_http",
        "@pip//opentelemetry_instrumentation_fastapi",
        "@pip//opentelemetry_sdk",
        "@pip//pgvector",
        "@pip//psycopg",
        "@pip//psycopg_binary",
        "@pip//pydantic",
        "@pip//pydantic_ai_slim",
        "@pip//pyyaml",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
        "@pip//trafilatura",
        "@pip//tzdata",
        "@pip//uvicorn",
        "@pip//youtube_transcript_api",
    ],
)

# nosemgrep: py-glob-missing-tests-exclude — this IS a testing library
py_library(
    name = "shared_testing",
    srcs = glob(["shared/testing/**/*.py"]),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        ":monolith_backend",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

tar(
    name = "knowledge_tools_tar",
    srcs = ["knowledge/tools/knowledge-search"],
    mtree = [
        "./usr/local/bin/knowledge-search type=file content=$(execpath knowledge/tools/knowledge-search) mode=0755 uid=0 gid=0",
    ],
)

py3_image(
    name = "image",
    binary = "//projects/monolith:main",
    env = {
        # py3_image sets PYTHONPATH to the workspace root (_main/) but this
        # project's sibling packages (app/, knowledge/) need the package directory
        # on the path too, matching the imports = ["."] used by py_venv_binary.
        "PYTHONPATH": "/projects/monolith/main.runfiles/_main:/projects/monolith/main.runfiles/_main/projects/monolith",
        # The minimal ubuntu base image has no CA certificates; point OpenSSL
        # at certifi's bundle so outbound HTTPS (e.g. Discord gateway) works.
        "SSL_CERT_FILE": "/projects/monolith/main.runfiles/_main/projects/monolith/.main/lib/python3.13/site-packages/certifi/cacert.pem",
    },
    main = "app/main.py",
    multiarch_tars = ["@claude_code//:tar"],
    repository = "ghcr.io/jomcgi/homelab/projects/monolith/backend",
    tars = [":knowledge_tools_tar"],
)

py_test(
    name = "markers_test",
    srcs = ["shared/testing/markers_test.py"],
    imports = ["."],
    deps = [
        ":shared_testing",
        "@pip//pytest",
    ],
)

py_test(
    name = "frontend_test",
    srcs = ["app/frontend_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
        "@pip//tzdata",
    ],
)

py_test(
    name = "home_schedule_test",
    srcs = ["home/schedule_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//httpx",
        "@pip//icalendar",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//tzdata",
    ],
)

py_test(
    name = "home_schedule_extra_test",
    srcs = ["home/schedule_extra_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//httpx",
        "@pip//icalendar",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//tzdata",
    ],
)

py_test(
    name = "home_schedule_router_test",
    srcs = ["home/schedule_router_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//tzdata",
    ],
)

py_test(
    name = "db_test",
    srcs = ["app/db_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "main_test",
    srcs = ["app/main_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
        "@pip//tzdata",
    ],
)

py_test(
    name = "integration_test",
    size = "medium",
    srcs = ["app/integration_test.py"],
    imports = ["."],
    tags = ["integration"],
    deps = [
        ":monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
        "@pip//tzdata",
    ],
)

py_test(
    name = "chat_models_test",
    srcs = ["chat/models_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_models_db_constraints_test",
    srcs = ["chat/models_db_constraints_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_models_coverage_test",
    srcs = ["chat/models_coverage_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "shared_embedding_test",
    srcs = ["shared/embedding_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "shared_embedding_retry_test",
    srcs = ["shared/embedding_retry_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "shared_embedding_batch_test",
    srcs = ["shared/embedding_batch_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_store_test",
    srcs = ["chat/store_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_web_search_test",
    srcs = ["chat/web_search_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_bot_test",
    srcs = ["chat/bot_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//discord_py",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_bot_send_message_test",
    srcs = ["chat/bot_send_message_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_agent_test",
    srcs = ["chat/agent_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_coverage_test",
    srcs = ["chat/store_coverage_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_on_message_test",
    srcs = ["chat/bot_on_message_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//discord_py",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_streaming_test",
    srcs = ["chat/bot_streaming_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//discord_py",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_web_search_coverage_test",
    srcs = ["chat/web_search_coverage_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "shared_embedding_coverage_test",
    srcs = ["shared/embedding_coverage_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_agent_deps_test",
    srcs = ["chat/agent_deps_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_agent_coverage_test",
    srcs = ["chat/agent_coverage_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "main_lifespan_discord_test",
    srcs = ["app/main_lifespan_discord_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
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
    name = "chat_agent_extra_test",
    srcs = ["chat/agent_extra_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_extra_test",
    srcs = ["chat/bot_extra_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//discord_py",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_web_search_extra_test",
    srcs = ["chat/web_search_extra_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_store_extra_test",
    srcs = ["chat/store_extra_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "main_otel_test",
    srcs = ["app/main_otel_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
        "@pip//tzdata",
    ],
)

py_test(
    name = "shared_embedding_extra_test",
    srcs = ["shared/embedding_extra_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_changelog_test",
    srcs = ["chat/changelog_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//discord_py",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_changelog_fetch_test",
    srcs = ["chat/changelog_fetch_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_summarizer_test",
    srcs = ["chat/summarizer_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_vault_export_test",
    srcs = ["chat/vault_export_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

py_test(
    name = "main_extra_test",
    srcs = ["app/main_extra_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
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
    name = "log_test",
    srcs = ["app/log_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "log_config_test",
    srcs = ["app/log_config_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "main_log_task_test",
    srcs = ["app/main_log_task_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "main_sidecar_test",
    srcs = ["app/main_sidecar_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
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
    name = "main_vault_sync_test",
    srcs = ["app/main_vault_sync_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//fastapi",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "store_integrity_test",
    srcs = ["chat/store_integrity_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "bot_self_message_test",
    srcs = ["chat/bot_self_message_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//discord_py",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "bot_embed_test",
    srcs = ["chat/bot_embed_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//discord_py",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "web_search_headers_test",
    srcs = ["chat/web_search_headers_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_models_summary_test",
    srcs = ["chat/models_summary_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_channel_summary_test",
    srcs = ["chat/store_channel_summary_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_summary_test",
    srcs = ["chat/store_summary_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_vision_errors_test",
    srcs = ["chat/vision_errors_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_summarizer_llm_caller_test",
    srcs = ["chat/summarizer_llm_caller_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_summarizer_channel_test",
    srcs = ["chat/summarizer_channel_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_exception_test",
    srcs = ["chat/bot_exception_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_bot_error_handling_test",
    srcs = ["chat/bot_error_handling_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//discord_py",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_agent_tools_test",
    srcs = ["chat/agent_tools_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_store_attachments_test",
    srcs = ["chat/store_attachments_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_agent_coerce_test",
    srcs = ["chat/agent_coerce_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_store_blob_test",
    srcs = ["chat/store_blob_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_flush_blob_test",
    srcs = ["chat/store_flush_blob_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_save_messages_test",
    srcs = ["chat/store_save_messages_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_embed_text_test",
    srcs = ["chat/store_embed_text_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_agent_tool_execution_test",
    srcs = ["chat/agent_tool_execution_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_attachments_test",
    srcs = ["chat/bot_attachments_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//discord_py",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_bot_multimodal_test",
    srcs = ["chat/bot_multimodal_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//discord_py",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_startup_test",
    srcs = ["chat/startup_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_summarizer_startup_test",
    srcs = ["chat/summarizer_startup_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_summarizer_prompts_test",
    srcs = ["chat/summarizer_prompts_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_vision_timeout_test",
    srcs = ["chat/vision_timeout_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_web_search_null_results_test",
    srcs = ["chat/web_search_null_results_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_models_persistence_test",
    srcs = ["chat/models_persistence_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_upsert_test",
    srcs = ["chat/store_upsert_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_bulk_test",
    srcs = ["chat/store_bulk_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_models_validation_test",
    srcs = ["chat/models_validation_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "app_main_summary_test",
    srcs = ["app/main_summary_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
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
    srcs = ["app/main_summary_edge_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
        "@pip//tzdata",
    ],
)

py_test(
    name = "scripts_red_dashboard_test",
    srcs = ["scripts/generate_red_dashboard_test.py"],
    data = ["scripts/generate-red-dashboard.py"],
    imports = ["."],
    deps = [
        "@pip//pytest",
    ],
)

py_test(
    name = "scripts_preview_layout_test",
    srcs = ["scripts/preview_layout_test.py"],
    data = ["scripts/preview-layout.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//networkx",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_agent_helper_functions_test",
    srcs = ["chat/agent_helper_functions_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_backfill_test",
    srcs = ["chat/backfill_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_vision_test",
    srcs = ["chat/vision_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_new_coverage_test",
    srcs = ["chat/chat_new_coverage_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_web_search_missing_fields_test",
    srcs = ["chat/web_search_missing_fields_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_store_non_integrity_error_test",
    srcs = ["chat/store_non_integrity_error_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_summarizer_none_id_test",
    srcs = ["chat/summarizer_none_id_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "shared_embedding_null_test",
    srcs = ["shared/embedding_null_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_bot_session_failure_test",
    srcs = ["chat/bot_session_failure_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//discord_py",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_bot_thinking_test",
    srcs = ["chat/bot_thinking_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//discord_py",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_bot_embeddable_content_test",
    srcs = ["chat/bot_embeddable_content_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//discord_py",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_agent_max_tokens_test",
    srcs = ["chat/agent_max_tokens_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_agent_signpost_test",
    srcs = ["chat/agent_signpost_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "shared_embedding_retryable_test",
    srcs = ["shared/embedding_retryable_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_vision_retryable_test",
    srcs = ["chat/vision_retryable_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_store_null_data_test",
    srcs = ["chat/store_null_data_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_auto_search_test",
    srcs = ["chat/bot_auto_search_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//discord_py",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_bot_response_storage_test",
    srcs = ["chat/bot_response_storage_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//discord_py",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "shared_embedding_connect_timeout_test",
    srcs = ["shared/embedding_connect_timeout_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_agent_web_search_tool_test",
    srcs = ["chat/agent_web_search_tool_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_bot_summary_injection_test",
    srcs = ["chat/bot_summary_injection_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_attachment_download_test",
    srcs = ["chat/bot_attachment_download_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//discord_py",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "e2e_test",
    size = "large",
    timeout = "moderate",
    srcs = [
        "e2e/__init__.py",
        "e2e/conftest.py",
        "e2e/e2e_test.py",
    ],
    data = [
        "//projects/monolith/chart:migrations",
        "@postgres_test//:postgres",
    ],
    env = {"PYTEST_ADDOPTS": "-p shared.testing.plugin"},
    imports = ["."],
    tags = ["e2e"],
    deps = [
        ":monolith_backend",
        ":shared_testing",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//psycopg",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
        "@pip//tzdata",
        "@pip//uvicorn",
    ],
)

py_test(
    name = "e2e_playwright_test",
    size = "large",
    timeout = "long",
    srcs = [
        "e2e/__init__.py",
        "e2e/conftest.py",
        "e2e/e2e_playwright_test.py",
    ],
    data = [
        ":frontend_dist",
        "//projects/monolith/chart:migrations",
        "@postgres_test//:postgres",
    ],
    env = {"PYTEST_ADDOPTS": "-p shared.testing.plugin"},
    imports = ["."],
    tags = [
        "e2e",
        "manual",
        "playwright",
    ],
    deps = [
        ":monolith_backend",
        ":shared_testing",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//psycopg",
        "@pip//pytest",
        # TODO: add @pip//pytest_playwright once requirements lock is regenerated
        "@pip//sqlmodel",
        "@pip//tzdata",
        "@pip//uvicorn",
    ],
)

py_test(
    name = "chat_router_test",
    srcs = ["chat/router_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_router_backfill_exception_test",
    srcs = ["chat/router_backfill_exception_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_backfill_flush_test",
    srcs = ["chat/backfill_flush_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_find_user_test",
    srcs = ["chat/store_find_user_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_search_model_validate_test",
    srcs = ["chat/store_search_model_validate_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_thinking_view_callback_test",
    srcs = ["chat/bot_thinking_view_callback_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//discord_py",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "scripts_red_dashboard_extra_test",
    srcs = ["scripts/generate_red_dashboard_extra_test.py"],
    data = ["scripts/generate-red-dashboard.py"],
    imports = ["."],
    deps = [
        "@pip//pytest",
    ],
)

py_test(
    name = "scripts_red_dashboard_coverage_test",
    srcs = ["scripts/generate_red_dashboard_coverage_test.py"],
    data = ["scripts/generate-red-dashboard.py"],
    imports = ["."],
    deps = [
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_store_lock_test",
    srcs = ["chat/store_lock_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_reprocess_test",
    srcs = ["chat/bot_reprocess_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//discord_py",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "main_lock_sweep_test",
    srcs = ["app/main_lock_sweep_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
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
    srcs = ["app/main_app_state_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
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
    name = "shared_scheduler_test",
    srcs = ["shared/scheduler_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "shared_scheduler_loop_test",
    srcs = ["shared/scheduler_loop_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "shared_scheduler_tick_missing_job_test",
    srcs = ["shared/scheduler_tick_missing_job_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "shared_scheduler_claim_test",
    srcs = ["shared/scheduler_claim_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "shared_scheduler_transitions_test",
    srcs = ["shared/scheduler_transitions_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "main_log_coverage_test",
    srcs = ["app/main_log_coverage_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
        "@pip//tzdata",
    ],
)

py_test(
    name = "home_schedule_calendar_handler_test",
    srcs = ["home/schedule_calendar_handler_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//icalendar",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//tzdata",
    ],
)

py_test(
    name = "shared_scheduler_loop_extra_test",
    srcs = ["shared/scheduler_loop_extra_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "shared_chunker_test",
    srcs = ["shared/chunker_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "shared_chunker_extra_test",
    srcs = ["shared/chunker_extra_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "shared_chunker_gaps_test",
    srcs = ["shared/chunker_gaps_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_frontmatter_test",
    srcs = ["knowledge/frontmatter_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

py_test(
    name = "knowledge_layout_test",
    srcs = ["knowledge/layout_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//networkx",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_links_test",
    srcs = ["knowledge/links_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_links_extra_test",
    srcs = ["knowledge/links_extra_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_links_coverage_test",
    srcs = ["knowledge/links_coverage_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_store_test",
    srcs = ["knowledge/store_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pydantic",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_reconciler_test",
    srcs = ["knowledge/reconciler_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_wikilinks_test",
    srcs = ["knowledge/wikilinks_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_visibility_test",
    srcs = ["knowledge/visibility_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_tools_add_visibility_field_test",
    srcs = ["knowledge/tools/add_visibility_field_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_tools_classify_visibility_backfill_test",
    srcs = ["knowledge/tools/classify_visibility_backfill_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_gardener_test",
    srcs = ["knowledge/gardener_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "gardener_distill_test",
    srcs = ["knowledge/gardener_distill_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "gardener_consolidation_test",
    srcs = ["knowledge/gardener_consolidation_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_reconciler_coverage_test",
    srcs = ["knowledge/reconciler_coverage_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_reconciler_extra_test",
    srcs = ["knowledge/reconciler_extra_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_models_test",
    srcs = ["knowledge/models_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_gap_model_test",
    srcs = ["knowledge/gap_model_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_gap_stubs_test",
    srcs = ["knowledge/gap_stubs_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_gap_classifier_test",
    srcs = ["knowledge/gap_classifier_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_gap_lifecycle_test",
    srcs = ["knowledge/gap_lifecycle_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_deleted_at_filter_test",
    srcs = ["knowledge/deleted_at_filter_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_gap_end_to_end_test",
    srcs = ["knowledge/gap_end_to_end_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_gap_logic_test",
    srcs = ["knowledge/gap_logic_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_gap_slug_resolution_test",
    srcs = ["knowledge/gap_slug_resolution_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_research_agent_test",
    srcs = ["knowledge/research_agent_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "knowledge_research_audit_trail_test",
    srcs = ["knowledge/research_audit_trail_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_research_writer_test",
    srcs = ["knowledge/research_writer_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

py_test(
    name = "knowledge_research_handler_test",
    srcs = ["knowledge/research_handler_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_research_end_to_end_test",
    srcs = ["knowledge/research_end_to_end_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//pyyaml",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_store_gap_queries_test",
    srcs = ["knowledge/store_gap_queries_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_service_test",
    srcs = ["knowledge/service_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//networkx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_gardener_coverage_test",
    srcs = ["knowledge/gardener_coverage_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_service_coverage_test",
    srcs = ["knowledge/service_coverage_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "knowledge_search_test",
    srcs = ["knowledge/knowledge_search_test.py"],
    data = ["knowledge/tools/knowledge-search"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "scripts_red_dashboard_gaps_test",
    srcs = ["scripts/generate_red_dashboard_gaps_test.py"],
    data = ["scripts/generate-red-dashboard.py"],
    imports = ["."],
    deps = [
        "@pip//pytest",
    ],
)

sh_binary(
    name = "generate-routes",
    srcs = ["generate-routes.sh"],
    visibility = ["//bazel/tools/format:__pkg__"],
)

py_test(
    name = "chat_backfill_extra_test",
    srcs = ["chat/backfill_extra_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "knowledge_store_extra_test",
    srcs = ["knowledge/store_extra_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_frontmatter_extra_test",
    srcs = ["knowledge/frontmatter_extra_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

py_test(
    name = "knowledge_frontmatter_json_safe_test",
    srcs = ["knowledge/frontmatter_json_safe_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

py_test(
    name = "chat_store_reclaim_race_test",
    srcs = ["chat/store_reclaim_race_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "home_schedule_naive_dtend_test",
    srcs = ["home/schedule_naive_dtend_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//icalendar",
        "@pip//pytest",
        "@pip//tzdata",
    ],
)

py_test(
    name = "knowledge_store_zero_chunk_test",
    srcs = ["knowledge/store_zero_chunk_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_store_optional_metadata_test",
    srcs = ["knowledge/store_optional_metadata_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_raw_ingest_test",
    srcs = ["knowledge/raw_ingest_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_raw_paths_test",
    srcs = ["knowledge/raw_paths_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_migrate_raw_bucketing_test",
    srcs = ["knowledge/migrate_raw_bucketing_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_fixes_coverage_test",
    srcs = ["knowledge/knowledge_fixes_coverage_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//pyyaml",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_router_test",
    srcs = ["knowledge/router_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_notes_crud_test",
    srcs = ["knowledge/notes_crud_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
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
    srcs = ["knowledge/mcp_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//fastmcp",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "knowledge_gap_api_test",
    srcs = ["knowledge/gap_api_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
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
    srcs = ["knowledge/gap_review_endpoints_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_note_review_endpoints_test",
    srcs = ["knowledge/note_review_endpoints_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
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
    srcs = ["knowledge/notes_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pyyaml",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_gaps_helpers_test",
    srcs = ["knowledge/gaps_helpers_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_research_handler_unit_test",
    srcs = ["knowledge/research_handler_unit_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_deleted_at_filter_test",
    srcs = ["knowledge/deleted_at_filter_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_mcp_gap_test",
    srcs = ["knowledge/mcp_gap_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//fastmcp",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//pyyaml",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "tasks_router_test",
    srcs = ["knowledge/tasks_router_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_ingest_queue_test",
    srcs = ["knowledge/ingest_queue_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "knowledge_store_note_links_test",
    srcs = ["knowledge/store_note_links_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_ingest_queue_extra_test",
    srcs = ["knowledge/ingest_queue_extra_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "knowledge_ingest_queue_claim_test",
    srcs = ["knowledge/ingest_queue_claim_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_raw_ingest_discover_test",
    srcs = ["knowledge/raw_ingest_discover_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_ingest_queue_webpage_title_test",
    srcs = ["knowledge/ingest_queue_webpage_title_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "knowledge_raw_ingest_move_phase_oserror_test",
    srcs = ["knowledge/raw_ingest_move_phase_oserror_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_web_search_type_error_test",
    srcs = ["chat/web_search_type_error_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "sse_test",
    srcs = ["chat/sse_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//anyio",
        "@pip//pytest",
    ],
)

py_test(
    name = "explorer_test",
    srcs = ["chat/explorer_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//anyio",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
    ],
)

py_test(
    name = "explore_router_test",
    srcs = ["chat/explore_router_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_ingest_queue_router_test",
    srcs = ["knowledge/ingest_queue_router_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_dead_letter_test",
    srcs = ["knowledge/dead_letter_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "tasks_store_test",
    srcs = ["knowledge/tasks_store_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "tasks_rollup_test",
    srcs = ["knowledge/tasks_rollup_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "home_observability_clickhouse_test",
    srcs = ["home/observability/clickhouse_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "home_observability_config_test",
    srcs = ["home/observability/config_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "home_observability_slo_test",
    srcs = ["home/observability/slo_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "home_observability_topology_config_test",
    srcs = ["home/observability/topology_config_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "home_observability_router_test",
    srcs = ["home/observability/router_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

semgrep_target_test(
    name = "main_semgrep_test",
    exclude_rules = [
        # keep
        "avoid-sqlalchemy-text",
        "generic-sql-fastapi",  # keep
        # The monolith uses datetime | None = None for intentional NULL state
        # fields (e.g. last_run_at, locked_at, updated_at). semgrep-core
        # invokes the rule engine directly without pysemgrep, so # nosemgrep
        # inline comments are not honoured. Exclude until per-finding
        # suppression is available in the core engine.
        "sqlmodel-datetime-without-factory",  # keep
        "tainted-fastapi-http-request-httpx",
        # note_id → DB lookup → note["path"] → file read is guarded by
        # is_relative_to(vault_root) but Semgrep Pro can't trace through
        # the DB as a sanitizer.
        "tainted-path-traversal-stdlib-fastapi",  # keep
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
    target = ":main",
)

py_test(
    name = "home_observability_stats_test",
    srcs = ["home/observability/stats_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "kubernetes_client_test",
    srcs = ["shared/kubernetes_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//kubernetes_asyncio",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "scheduler_views_test",
    srcs = ["scheduler/views_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pydantic",
        "@pip//pytest",
    ],
)

py_test(
    name = "scheduler_service_test",
    srcs = ["scheduler/service_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "scheduler_router_test",
    srcs = ["scheduler/router_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

bdd_test(
    name = "home_bdd_api_test",
    srcs = [
        "home/tests/__init__.py",
        "home/tests/bdd_api_test.py",
        "home/tests/conftest.py",
    ],
)

bdd_test(
    name = "home_bdd_playwright_test",
    timeout = "long",
    srcs = [
        "home/tests/__init__.py",
        "home/tests/bdd_playwright_test.py",
        "home/tests/conftest.py",
    ],
    playwright = True,
)

bdd_test(
    name = "home_bdd_public_test",
    srcs = [
        "home/tests/__init__.py",
        "home/tests/bdd_public_test.py",
        "home/tests/conftest.py",
    ],
)

bdd_test(
    name = "chat_bdd_api_test",
    srcs = [
        "chat/tests/__init__.py",
        "chat/tests/bdd_api_test.py",
        "chat/tests/conftest.py",
    ],
)

bdd_test(
    name = "knowledge_bdd_api_test",
    srcs = [
        "knowledge/tests/__init__.py",
        "knowledge/tests/bdd_api_test.py",
        "knowledge/tests/conftest.py",
    ],
)

bdd_test(
    name = "knowledge_bdd_playwright_test",
    timeout = "long",
    srcs = [
        "knowledge/tests/__init__.py",
        "knowledge/tests/bdd_playwright_test.py",
        "knowledge/tests/conftest.py",
    ],
    playwright = True,
)

bdd_test(
    name = "knowledge_bdd_public_test",
    srcs = [
        "knowledge/tests/__init__.py",
        "knowledge/tests/bdd_public_test.py",
        "knowledge/tests/conftest.py",
    ],
)

bdd_test(
    name = "shared_bdd_scheduler_test",
    srcs = [
        "shared/tests/__init__.py",
        "shared/tests/bdd_scheduler_test.py",
        "shared/tests/conftest.py",
    ],
)

py_test(
    name = "bdd_completeness_test",
    size = "small",
    srcs = [
        "app/__init__.py",
        "app/bdd_completeness_test.py",
    ],
    data = glob(["*/tests/*_test.py"]),
    env = {"PYTEST_ADDOPTS": "--import-mode=importlib --ignore=projects/monolith/home/tests --ignore=projects/monolith/chat/tests --ignore=projects/monolith/knowledge/tests --ignore=projects/monolith/shared/tests --ignore=projects/monolith/agent/tests"},
    imports = ["."],
    tags = ["bdd"],
    deps = [
        ":monolith_backend",
        ":shared_testing",
        "@pip//pytest",
    ],
)

py_test(
    name = "agent_config_test",
    srcs = ["agent/config_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "agent_notify_test",
    srcs = ["agent/notify_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

bdd_test(
    name = "agent_bdd_locks_test",
    srcs = [
        "agent/tests/__init__.py",
        "agent/tests/bdd_locks_test.py",
        "agent/tests/conftest.py",
    ],
)

bdd_test(
    name = "agent_bdd_routine_jobs_test",
    srcs = [
        "agent/tests/__init__.py",
        "agent/tests/bdd_routine_jobs_test.py",
        "agent/tests/conftest.py",
    ],
)

bdd_test(
    name = "agent_bdd_checks_test",
    srcs = [
        "agent/tests/__init__.py",
        "agent/tests/bdd_checks_test.py",
        "agent/tests/conftest.py",
    ],
)

py_test(
    name = "agent_checks_firing_alerts_test",
    srcs = ["agent/checks_firing_alerts_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "agent_mcp_test",
    srcs = ["agent/mcp_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//fastmcp",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "agent_mcp_description_compliance_test",
    srcs = ["agent/mcp_description_compliance_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//fastmcp",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

semgrep_target_test(
    name = "add_visibility_field_semgrep_test",
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
    target = ":add_visibility_field",
)

semgrep_target_test(
    name = "classify_visibility_backfill_semgrep_test",
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
    target = ":classify_visibility_backfill",
)
