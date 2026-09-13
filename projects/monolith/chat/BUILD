# Bazel-native package ownership. Classification is declared here, not in
# projects/monolith/BUILD.
# This suite uses the BDD harness and is owned by the explicit bdd_test below.
# gazelle:exclude tests/bdd_api_test.py

load("@aspect_rules_py//py:defs.bzl", "py_library")
load("//bazel/tools/pytest:defs.bzl", "py_test")
load("//projects/monolith:bdd_test.bzl", "bdd_test")

package(default_visibility = ["//visibility:private"])

exports_files(
    glob(["**/*.py"]),
    visibility = ["//projects/monolith:private_backend_consumers"],
)

py_library(
    name = "chat",
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
    deps = [
        "//projects/monolith/agent_sessions:agent_sessions_admission",
        "//projects/monolith/artifact",
        "//projects/monolith/cluster",
        "//projects/monolith/core",
        "//projects/monolith/framework",
        "//projects/monolith/knowledge",
        "//projects/monolith/sandbox",
        "//projects/monolith/scheduler",
        "//projects/monolith/shared",
        "@pip//boto3",
        "@pip//certifi",
        "@pip//discord_py",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//icalendar",
        "@pip//opentelemetry_exporter_otlp_proto_http",
        "@pip//opentelemetry_instrumentation_fastapi",
        "@pip//opentelemetry_instrumentation_httpx",
        "@pip//opentelemetry_sdk",
        "@pip//pgvector",
        "@pip//psycopg",
        "@pip//psycopg_binary",
        "@pip//pydantic",
        "@pip//pydantic_ai_slim",
        "@pip//python_multipart",
        "@pip//pyyaml",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
        "@pip//tzdata",
        "@pip//uvicorn",
    ],
)

py_test(
    name = "chat_models_test",
    srcs = ["models_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_models_db_constraints_test",
    srcs = ["models_db_constraints_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_models_reminder_test",
    srcs = ["models_reminder_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_reminders_test",
    srcs = ["reminders_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_jobs_reminder_test",
    srcs = ["jobs_reminder_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_models_coverage_test",
    srcs = ["models_coverage_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_test",
    srcs = ["store_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_query_stats_test",
    srcs = ["query_stats_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_fetch_window_test",
    srcs = ["store_fetch_window_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_digest_test",
    srcs = ["digest_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_channel_data_test",
    srcs = ["channel_data_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_observer_test",
    srcs = ["observer_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_observer_job_test",
    srcs = ["observer_job_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_outbox_test",
    srcs = ["outbox_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_whatsapp_outbox_test",
    srcs = ["whatsapp_outbox_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_whatsapp_group_test",
    srcs = ["whatsapp_group_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_whatsapp_inbound_test",
    srcs = ["whatsapp_inbound_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_whatsapp_timeparse_test",
    srcs = ["whatsapp_timeparse_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_whatsapp_intents_test",
    srcs = ["whatsapp_intents_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_whatsapp_calendar_test",
    srcs = ["whatsapp_calendar_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_whatsapp_capabilities_test",
    srcs = ["whatsapp_capabilities_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_whatsapp_digest_test",
    srcs = ["whatsapp_digest_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_orchestrator_bundle_test",
    srcs = ["orchestrator_bundle_test.py"],
    # The test reads both the hand-written base prompt and the committed
    # generated bundle directly from runfiles (Path(__file__).parent), so ship
    # them as data (mirrors chat_directives_test's directive_seed.md). The
    # generator itself is exercised via its pure functions only: the git
    # ls-files-driven path discovery has no .git to walk inside the bazel test
    # sandbox, same reason gen_repo_docs_manifest_test.py never calls
    # iter_doc_paths.
    data = [
        "orchestrator_bundle.md",
        "orchestrator_prompt.md",
    ],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "//projects/monolith/knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_acl_test",
    srcs = ["acl_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_attention_log_test",
    srcs = ["attention_log_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_directives_test",
    srcs = ["directives_test.py"],
    # directives.get_active reads the committed seed from runfiles via
    # Path(__file__).parent / "directive_seed.md", so ship it as test data
    # (mirrors campsites_client_test's catalog.json).
    data = ["directive_seed.md"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_ambient_analysis_test",
    srcs = ["ambient_analysis_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_autopilot_job_test",
    srcs = ["autopilot_job_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_attention_test",
    srcs = ["attention_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_reply_sanitize_test",
    srcs = ["reply_sanitize_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_web_search_test",
    srcs = ["web_search_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_bot_test",
    srcs = ["bot_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_bot_send_message_test",
    srcs = ["bot_send_message_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_agent_test",
    srcs = ["agent_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_coverage_test",
    srcs = ["store_coverage_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_on_message_test",
    srcs = ["bot_on_message_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "//projects/monolith/agent_sessions",
        "@pip//discord_py",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_streaming_test",
    srcs = ["bot_streaming_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_web_search_coverage_test",
    srcs = ["web_search_coverage_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_agent_deps_test",
    srcs = ["agent_deps_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_agent_coverage_test",
    srcs = ["agent_coverage_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_agent_extra_test",
    srcs = ["agent_extra_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_extra_test",
    srcs = ["bot_extra_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_web_search_extra_test",
    srcs = ["web_search_extra_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_store_extra_test",
    srcs = ["store_extra_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_changelog_test",
    srcs = ["changelog_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_changelog_fetch_test",
    srcs = ["changelog_fetch_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_summarizer_test",
    srcs = ["summarizer_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "store_integrity_test",
    srcs = ["store_integrity_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "bot_self_message_test",
    srcs = ["bot_self_message_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "bot_embed_test",
    srcs = ["bot_embed_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "web_search_headers_test",
    srcs = ["web_search_headers_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_models_summary_test",
    srcs = ["models_summary_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_channel_summary_test",
    srcs = ["store_channel_summary_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_summary_test",
    srcs = ["store_summary_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_vision_errors_test",
    srcs = ["vision_errors_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_orchestrator_client_test",
    srcs = ["orchestrator_client_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_orchestrator_plan_test",
    srcs = ["orchestrator_plan_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_orchestrator_test",
    srcs = ["orchestrator_test.py"],
    # compile() reads the committed bundle from runfiles (Path(__file__).parent),
    # so ship it as data (mirrors chat_orchestrator_bundle_test).
    data = ["orchestrator_bundle.md"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_summarizer_llm_caller_test",
    srcs = ["summarizer_llm_caller_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_summarizer_agent_reply_test",
    srcs = ["summarizer_agent_reply_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_agent_prompt_echo_test",
    srcs = ["bot_agent_prompt_echo_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "//projects/monolith/agent_sessions",
        "@pip//discord_py",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_summarizer_channel_test",
    srcs = ["summarizer_channel_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_exception_test",
    srcs = ["bot_exception_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_bot_error_handling_test",
    srcs = ["bot_error_handling_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_agent_tools_test",
    srcs = ["agent_tools_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_store_attachments_test",
    srcs = ["store_attachments_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_agent_coerce_test",
    srcs = ["agent_coerce_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_store_blob_test",
    srcs = ["store_blob_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_flush_blob_test",
    srcs = ["store_flush_blob_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_save_messages_test",
    srcs = ["store_save_messages_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_embed_text_test",
    srcs = ["store_embed_text_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_agent_tool_execution_test",
    srcs = ["agent_tool_execution_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_agent_channel_digest_tool_test",
    srcs = ["agent_channel_digest_tool_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_agent_reminder_tools_test",
    srcs = ["agent_reminder_tools_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_attachments_test",
    srcs = ["bot_attachments_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_bot_multimodal_test",
    srcs = ["bot_multimodal_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_startup_test",
    srcs = ["startup_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "//projects/monolith/scheduler",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_summarizer_startup_test",
    srcs = ["summarizer_startup_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "//projects/monolith/scheduler",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_summarizer_prompts_test",
    srcs = ["summarizer_prompts_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_vision_timeout_test",
    srcs = ["vision_timeout_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_web_search_null_results_test",
    srcs = ["web_search_null_results_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_models_persistence_test",
    srcs = ["models_persistence_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_upsert_test",
    srcs = ["store_upsert_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_bulk_test",
    srcs = ["store_bulk_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_models_validation_test",
    srcs = ["models_validation_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_agent_helper_functions_test",
    srcs = ["agent_helper_functions_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_backfill_test",
    srcs = ["backfill_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_vision_test",
    srcs = ["vision_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_new_coverage_test",
    srcs = ["chat_new_coverage_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
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
    srcs = ["web_search_missing_fields_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_store_non_integrity_error_test",
    srcs = ["store_non_integrity_error_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_summarizer_none_id_test",
    srcs = ["summarizer_none_id_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_session_failure_test",
    srcs = ["bot_session_failure_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_bot_thinking_test",
    srcs = ["bot_thinking_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_bot_embeddable_content_test",
    srcs = ["bot_embeddable_content_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_agent_max_tokens_test",
    srcs = ["agent_max_tokens_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_agent_signpost_test",
    srcs = ["agent_signpost_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_vision_retryable_test",
    srcs = ["vision_retryable_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_store_null_data_test",
    srcs = ["store_null_data_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_auto_search_test",
    srcs = ["bot_auto_search_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_bot_response_storage_test",
    srcs = ["bot_response_storage_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_agent_web_search_tool_test",
    srcs = ["agent_web_search_tool_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_bot_summary_injection_test",
    srcs = ["bot_summary_injection_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_identity_note_test",
    srcs = ["bot_identity_note_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_subject_note_test",
    srcs = ["bot_subject_note_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_attachment_download_test",
    srcs = ["bot_attachment_download_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_router_test",
    srcs = ["router_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_router_backfill_exception_test",
    srcs = ["router_backfill_exception_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_backfill_flush_test",
    srcs = ["backfill_flush_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_find_user_test",
    srcs = ["store_find_user_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_search_model_validate_test",
    srcs = ["store_search_model_validate_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_thinking_view_callback_test",
    srcs = ["bot_thinking_view_callback_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_store_lock_test",
    srcs = ["store_lock_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_reprocess_test",
    srcs = ["bot_reprocess_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_backfill_extra_test",
    srcs = ["backfill_extra_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_store_reclaim_race_test",
    srcs = ["store_reclaim_race_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_web_search_type_error_test",
    srcs = ["web_search_type_error_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "sse_test",
    srcs = ["sse_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//anyio",
        "@pip//pytest",
    ],
)

py_test(
    name = "explorer_test",
    srcs = ["explorer_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "@pip//anyio",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
    ],
)

py_test(
    name = "explore_router_test",
    srcs = ["explore_router_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "//projects/monolith/framework",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
    ],
)

py_test(
    name = "cluster_agent_test",
    srcs = ["cluster_agent_test.py"],
    imports = [".."],
    deps = [
        "//projects/monolith:pkg_cyclegroup",
        "//projects/monolith/cluster",
        "@pip//anyio",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
    ],
)

bdd_test(
    name = "chat_bdd_api_test",
    srcs = [
        "tests/__init__.py",
        "tests/bdd_api_test.py",
        "tests/conftest.py",
    ],
)

# keep
py_library(
    name = "agents",
    srcs = glob(
        [
            "models.py",
            "outbox.py",
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
    name = "runtime_data",
    srcs = [
        "directive_seed.md",
        "orchestrator_bundle.md",
    ],
    visibility = ["//projects/monolith:__pkg__"],
)
