# The frontend is pnpm/rules_js with hand-written BUILD files, so gazelle must
# not walk it. frontend/BUILD already carries a `# gazelle:ignore`, but that
# directive applies ONLY to the directory it appears in and does NOT recurse:
# gazelle still descended into src/routes/ and generated ts_project targets for
# SvelteKit route directories, which have no tsconfig.json. Those targets fail
# at load time ("No tsconfig.json file found"), which poisons `//...` evaluation
# and makes `ci` unable to start any build at all. Excluding from here is what
# every other subdirectory above does, and unlike `ignore` it covers the subtree.
# gazelle:exclude frontend
# Hand-written bdd_test targets (need the real-Postgres harness), so keep gazelle
# from generating conflicting py_tests for them.
# gazelle:exclude public_reader_grants_test.py
# gazelle:exclude observability_snapshot_grants_test.py
# gazelle:exclude public_knowledge_views_test.py
# gazelle:exclude chat_public_grants_test.py
# gazelle:exclude public_httproute_chat_guard_test.py
# gazelle:exclude public_api_chunks_grants_test.py
# gazelle:exclude public_turnstile_secret_isolation_test.py
# gazelle:exclude ember_public_savings_grants_test.py
# gazelle:exclude ember_synthetic_probe_grants_test.py
# gazelle:semgrep_exclude_rules avoid-sqlalchemy-text,sqlmodel-datetime-without-factory,tainted-fastapi-http-request-httpx,tainted-path-traversal-stdlib-fastapi,tainted-path-traversal-pillow-fastapi
load("@aspect_rules_py//py:defs.bzl", "py_library")
load("@aspect_rules_py//py/private/py_venv:defs.bzl", "py_venv_binary")
load("@rules_pkg//pkg:tar.bzl", "pkg_tar")
load("@rules_shell//shell:sh_binary.bzl", "sh_binary")
load("//bazel/semgrep/defs:defs.bzl", "semgrep_target_test", "semgrep_test")
load("//bazel/tools/js:exec_filegroup.bzl", "exec_filegroup")
load("//bazel/tools/oci:py3_image.bzl", "py3_image")
load("//bazel/tools/pytest:defs.bzl", "py_test")
load("//projects/monolith:bdd_test.bzl", "bdd_test")
load("//projects/monolith:domain_images.bzl", "MONOLITH_DOMAINS", "monolith_domain_images")

# Pin the frontend build to the exec (host) platform. The vite output is
# just static HTML/CSS/JS — it's arch-independent — but py3_image's
# platform transitions would otherwise cause aspect_rules_js to select
# wrong-arch native binaries (esbuild, rollup) for the arm64 variant.
exec_filegroup(
    name = "frontend_dist",
    src = "//projects/monolith/frontend:build",
)

# Module config.py files for chart validation test.
filegroup(
    name = "module_configs",
    srcs = [
        "//projects/monolith/agent:config.py",
        "//projects/monolith/swarm:config.py",
    ],
    visibility = ["//projects/monolith/chart:__pkg__"],
)

# framework/core.py as DATA, for chart/otel_endpoint_test.py. That test parses
# it rather than importing it: it needs the OTel env var name and the private
# profile's service_name as they are literally written, and importing the
# module would pull in the whole FastAPI/pydantic closure to read two strings.
filegroup(
    name = "framework_core_src",
    srcs = ["//projects/monolith/framework:core.py"],
    visibility = ["//projects/monolith/chart:__pkg__"],
)

py_venv_binary(
    name = "main",
    srcs = ["//projects/monolith/app:main.py"],  # keep
    data = [
        # ratings.py reads this committed Elo snapshot at runtime via
        # Path(__file__).parent/"ratings"/"elo_2026.json"; the "worldcup/**/*.py"
        # glob does not capture the .json, so ship it explicitly into runfiles.
        "//projects/monolith/worldcup:ratings/elo_2026.json",
        # campsites.client.load_catalog reads this committed static catalog at
        # runtime via importlib.resources.files("campsites")/"catalog.json"; the
        # "campsites/**/*.py" glob does not capture the .json, so ship it into
        # runfiles explicitly (mirrors the worldcup Elo snapshot above).
        "//projects/monolith/campsites:catalog.json",
        # directives.get_active reads this committed base directive at runtime
        # via Path(__file__).parent / "directive_seed.md" (ADR 035 phase 5); the
        # "chat/**/*.py" glob does not capture the .md, so ship it explicitly.
        "//projects/monolith/chat:directive_seed.md",
        # orchestrator.load_bundle reads this committed baked context bundle at
        # runtime via Path(__file__).with_name("orchestrator_bundle.md") (ADR
        # 036); the "chat/**/*.py" glob does not capture the .md, so ship it
        # explicitly or every escalation fails open with FileNotFoundError.
        "//projects/monolith/chat:orchestrator_bundle.md",
        # loadtest_corpus.load_corpus reads these committed .sample files at
        # runtime via Path(__file__).parent; the "demos/**/*.py" glob does not
        # capture non-.py data, so ship them explicitly into runfiles (mirrors
        # the worldcup Elo snapshot and campsites catalog above).
        ":frontend_dist",
        "//projects/monolith/demos:loadtest_samples",
    ],
    imports = ["."],  # keep
    main = "//projects/monolith/app:main.py",
    visibility = ["//:__subpackages__"],
    deps = [":monolith_backend"],  # keep
)

# The repo-docs manifest (3.3 MB, regenerated by every docs/CLAUDE.md/skill
# edit) ships as its OWN image layer rather than as `data` on the binaries.
# py_image_layer feeds the binary's whole runfiles tree into every layer tar,
# so while the manifest was runfiles a docs-only PR rebuilt all of
# :image/:jobs_image's layers and images (27 Tar + 7 OCIImage actions on a
# one-line YAML PR, candidate aa4f95a1, 2026-08-22). As a separate tar only
# this layer and the final image assembly redo. repo_docs.manifest_path()
# reads REPO_DOCS_MANIFEST_PATH, set in the image env below; tests keep the
# file in their own `data`.
pkg_tar(
    name = "repo_docs_manifest_layer",
    srcs = ["//projects/monolith/knowledge:repo_docs_manifest.ndjson"],
    package_dir = "/etc/monolith",
)

# Batch-jobs entrypoint: same source closure and deps as :main, but launches
# app/jobs_main.py (a Typer CLI) instead of the FastAPI server. Built into its
# own image (:jobs_image) so an ephemeral pod (e.g. an Argo Workflow) can run a
# single job to completion off the API pod. The jobs entrypoint never serves
# the frontend, so the static bundle is omitted. The repo-docs manifest, which
# the knowledge.repo_docs_reconcile job reads, arrives as the
# :repo_docs_manifest_layer tar on :jobs_image rather than as data here.
py_venv_binary(
    name = "jobs",
    srcs = ["//projects/monolith/app:jobs_main.py"],  # keep
    data = [
        "//projects/monolith/worldcup:ratings/elo_2026.json",
        # The campsites-refresh job (campsites.jobs.refresh_handler) reads this
        # static catalog from runfiles via campsites.client.load_catalog, so it
        # must ship in the jobs image too (data on :main does not propagate to
        # this separate binary).
        "//projects/monolith/campsites:catalog.json",
    ],
    imports = ["."],  # keep
    main = "//projects/monolith/app:jobs_main.py",
    visibility = ["//:__subpackages__"],
    # typer is the jobs entrypoint's CLI framework but is NOT a serving dep, so
    # it is absent from :monolith_backend; the binary must depend on it directly
    # or the jobs image fails at startup with `ModuleNotFoundError: typer`.
    deps = [
        ":monolith_backend",
        "@pip//typer",
    ],  # keep
)

# Progress-ingest entrypoint (ADR agents/051): same source closure as :main,
# launching app/progress_main.py, a single-route FastAPI listener on :8091.
# Its own binary/image (mirroring :jobs) because the EmberVM egress allowlist
# is host:port granular: this port must expose exactly one write-only route,
# so it cannot share the main API's process or port. No data files: the
# listener touches only agent_sessions store/models and core.db.
py_venv_binary(
    name = "progress",
    srcs = ["//projects/monolith/app:progress_main.py"],  # keep
    imports = ["."],  # keep
    main = "//projects/monolith/app:progress_main.py",
    visibility = ["//:__subpackages__"],
    deps = [":monolith_backend"],  # keep
)

# Phase-1 vault-mutation script: walk a vault and inject `visibility:` lines
# into note frontmatter that lacks the key. Run via `bazel run`:
#   bazel run //projects/monolith:add_visibility_field -- \
#       --vault-root /path/to/vault --dirs _processed
py_venv_binary(
    name = "add_visibility_field",
    main = "//projects/monolith/knowledge:tools/add_visibility_field.py",
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
    main = "//projects/monolith/knowledge:tools/classify_visibility_backfill.py",
    visibility = ["//:__subpackages__"],
)

# Stdlib-only generator: walks the repo tree (under BUILD_WORKSPACE_DIRECTORY
# when run via `bazel run`) and writes the committed repo-docs manifest NDJSON.
# Regenerates projects/monolith/knowledge/repo_docs_manifest.ndjson. Run via
# `bazel run //projects/monolith:gen_repo_docs_manifest`; the manifest staleness
# check in bazel/images/validate-generate-scripts.sh runs it in CI. No `imports`
# attr: it is a standalone stdlib script (like add_visibility_field), and
# imports = ["."] would repoint the venv main and break `bazel run`.
py_venv_binary(
    name = "gen_repo_docs_manifest",
    main = "//projects/monolith/knowledge:tools/gen_repo_docs_manifest.py",
    visibility = ["//:__subpackages__"],
)

# Stdlib-only generator for the PUBLIC docs manifest consumed by the SvelteKit
# /docs route. Publishes up to four current-state docs for each explicitly
# allowlisted public project and writes the committed
# projects/monolith/frontend/src/lib/public/docs/docs-manifest.json. Run via
# `bazel run //projects/monolith:gen_docs_manifest`. Same no
# `imports` attr rationale as gen_repo_docs_manifest above.
py_venv_binary(
    name = "gen_docs_manifest",
    main = "//projects/monolith/knowledge:tools/gen_docs_manifest.py",
    visibility = ["//:__subpackages__"],
)

py_venv_binary(
    name = "gen_posts_manifest",
    main = "//projects/monolith/knowledge:tools/gen_posts_manifest.py",
    visibility = ["//:__subpackages__"],
)

# Stdlib-only generator for the ADR 036 orchestrator baked context bundle.
# Concatenates chat/orchestrator_prompt.md, the recipe catalog under
# goosecracker/recipes/*.yaml, and a repo structure
# digest into the committed chat/orchestrator_bundle.md, byte-deterministic (git
# ls-files driven, no timestamps) so the orchestrator's system-message prefix
# stays cache-stable across escalations. Run via
# `bazel run //projects/monolith:gen_orchestrator_bundle`; wired into
# bazel/tools/format/run-generators.sh alongside the doc manifests so the Format
# check action auto-commits drift. Same no `imports` attr rationale as
# gen_repo_docs_manifest above.
py_venv_binary(
    name = "gen_orchestrator_bundle",
    main = "//projects/monolith/knowledge:tools/gen_orchestrator_bundle.py",
    visibility = ["//:__subpackages__"],
)

# Python package targets live with their source trees. This group admits the
# private backend, compatibility aliases, and peer domain packages while
# excluding the public aggregator package. Public membership is exposed only
# through each package's explicitly tagged :public target.
package_group(
    name = "package_consumers",
    packages = [
        "-//projects/monolith/public_backend",
        "//projects/monolith",
        "//projects/monolith/...",
    ],
)

# Compatibility aliases keep the existing hand-written tests and downstream
# labels stable while source ownership moves into the package directories.
alias(
    name = "pkg_auth",
    actual = "//projects/monolith/auth:pkg_auth",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_core",
    actual = "//projects/monolith/core:pkg_core",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_shared",
    actual = "//projects/monolith/shared:pkg_shared",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_framework",
    actual = "//projects/monolith/framework:pkg_framework",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_artifact",
    actual = "//projects/monolith/artifact:pkg_artifact",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_scheduler",
    actual = "//projects/monolith/scheduler:pkg_scheduler",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_cluster",
    actual = "//projects/monolith/cluster:pkg_cluster",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_trips",
    actual = "//projects/monolith/trips:pkg_trips",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_faas",
    actual = "//projects/monolith/faas:pkg_faas",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_sandbox",
    actual = "//projects/monolith/sandbox:pkg_sandbox",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_shotter",
    actual = "//projects/monolith/shotter:pkg_shotter",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_semgrep_scan",
    actual = "//projects/monolith/semgrep_scan:pkg_semgrep_scan",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_campsites",
    actual = "//projects/monolith/campsites:pkg_campsites",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_ships",
    actual = "//projects/monolith/ships:pkg_ships",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_worldcup",
    actual = "//projects/monolith/worldcup:pkg_worldcup",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_knowledge",
    actual = "//projects/monolith/knowledge:pkg_knowledge",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_hikes",
    actual = "//projects/monolith/hikes:pkg_hikes",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_moving",
    actual = "//projects/monolith/moving:pkg_moving",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_updates",
    actual = "//projects/monolith/updates:pkg_updates",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_stars",
    actual = "//projects/monolith/stars:pkg_stars",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_chat_public",
    actual = "//projects/monolith/chat_public:pkg_chat_public",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_grimoire",
    actual = "//projects/monolith/grimoire:pkg_grimoire",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_grimoire_chat",
    actual = "//projects/monolith/grimoire_chat:pkg_grimoire_chat",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_ember_public",
    actual = "//projects/monolith/ember_public:pkg_ember_public",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_cyclegroup",
    actual = "//projects/monolith/agent:pkg_cyclegroup",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "agent_sessions_rationale",
    actual = "//projects/monolith/agent_sessions:agent_sessions_rationale",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "agent_sessions_admission",
    actual = "//projects/monolith/agent_sessions:agent_sessions_admission",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_agent_sessions",
    actual = "//projects/monolith/agent_sessions:pkg_agent_sessions",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "swarm_rationale",
    actual = "//projects/monolith/swarm:swarm_rationale",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "swarm_factory_controls",
    actual = "//projects/monolith/swarm:swarm_factory_controls",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_swarm",
    actual = "//projects/monolith/swarm:pkg_swarm",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_dr_jobs",
    actual = "//projects/monolith/dr_jobs:pkg_dr_jobs",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "pkg_demos",
    actual = "//projects/monolith/demos:pkg_demos",
    visibility = ["//:__subpackages__"],
)

py_library(
    name = "monolith_backend",
    # entities.yaml is 2.5 KB and deliberately ships as runfiles data, unlike
    # the multi-megabyte ndjson manifest isolated in its own image layer.
    data = ["//projects/monolith/knowledge:entities.yaml"],
    visibility = ["//:__subpackages__"],
    deps = [
        "//projects/monolith/agent:pkg_cyclegroup",
        "//projects/monolith/agent_sessions:pkg_agent_sessions",
        "//projects/monolith/app:pkg_app",
        "//projects/monolith/auth:pkg_auth",
        "//projects/monolith/core:pkg_core",
        "//projects/monolith/framework:pkg_framework",
        "//projects/monolith/artifact:pkg_artifact",
        "//projects/monolith/chat_public:pkg_chat_public",
        "//projects/monolith/cluster:pkg_cluster",
        "//projects/monolith/demos:pkg_demos",
        "//projects/monolith/ember_public:pkg_ember_public",
        "//projects/monolith/faas:pkg_faas",
        "//projects/monolith/knowledge:pkg_knowledge",
        "//projects/monolith/observability:pkg_observability",
        "//projects/monolith/sandbox:pkg_sandbox",
        "//projects/monolith/scheduler:pkg_scheduler",
        "//projects/monolith/semgrep_scan:pkg_semgrep_scan",
        "//projects/monolith/shared:pkg_shared",
        "//projects/monolith/ships:pkg_ships",
        "//projects/monolith/hikes:pkg_hikes",
        "//projects/monolith/moving:pkg_moving",
        "//projects/monolith/stars:pkg_stars",
        "//projects/monolith/trips:pkg_trips",
        "//projects/monolith/dr_jobs:pkg_dr_jobs",
        "//projects/monolith/worldcup:pkg_worldcup",
        "//projects/monolith/campsites:pkg_campsites",
        "//projects/monolith/grimoire:pkg_grimoire",
        "//projects/monolith/grimoire_chat:pkg_grimoire_chat",
        "//projects/monolith/shotter:pkg_shotter",
        "//projects/monolith/swarm:pkg_swarm",
        "//projects/monolith/updates:pkg_updates",
        "@pip//dbos",
        "@pip//astral",
        "@pip//beautifulsoup4",
        "@pip//boto3",
        "@pip//certifi",
        "@pip//curl_cffi",  # campsites/client.py (browser-TLS impersonation vs BC Parks WAF)
        "@pip//defusedxml",  # trips/transform.py (KML parsing)
        "@pip//discord_py",
        "@pip//fastapi",
        "@pip//fastmcp",
        "@pip//genai_prices",
        "@pip//httpx",
        "@pip//icalendar",
        "@pip//kubernetes_asyncio",
        "@pip//lxml_html_clean",  # explicit: overrides.txt strips lxml[html-clean] extra
        # shotter/mcp.py imports mcp.types.ImageContent directly (ADR
        # embervm/035 section 5); every other domain reaches the mcp package
        # only transitively through fastmcp.
        "@pip//mcp",
        "@pip//networkx",
        "@pip//opentelemetry_exporter_otlp_proto_http",
        "@pip//opentelemetry_instrumentation_fastapi",
        "@pip//opentelemetry_instrumentation_httpx",
        "@pip//opentelemetry_sdk",
        "@pip//pgvector",
        "@pip//pillow",  # trips/exif.py (PIL image EXIF)
        "@pip//psycopg",
        "@pip//psycopg_binary",
        "@pip//pydantic",
        "@pip//pydantic_ai_slim",
        "@pip//python_multipart",  # trips/ingest_router.py (UploadFile / form parsing)
        "@pip//pyyaml",
        # PyJWT is currently locked transitively through mcp and semgrep.
        "@pip//pyjwt",
        # NOTE: semgrep_scan/report.py uploads to the Semgrep App via plain
        # authenticated HTTP (explicit Authorization: Bearer) using httpx (already
        # a dep above), not ScanHandler.
        # pysemgrep INTERNAL APIs for the fc-invoke -> Semgrep-App relay
        # (semgrep_scan/report.py): only the out.* atd types (light) + __VERSION__.
        # We do NOT use ScanHandler / get_state / app_session (auth never attaches
        # outside a click command, and start_scan polls the OOM /config ruleset).
        # Pinned 1.168.0 in bazel/requirements/tools.in.
        "@pip//semgrep",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
        "@pip//starlette",
        "@pip//timelength",
        "@pip//trafilatura",
        "@pip//tzdata",
        "@pip//uvicorn",
        "@pip//websockets",
        "@pip//youtube_transcript_api",
    ],
)

# nosemgrep: py-glob-missing-tests-exclude — this IS a testing library
alias(
    name = "shared_testing",
    actual = "//projects/monolith/shared:shared_testing",
    visibility = ["//:__subpackages__"],
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
        "REPO_DOCS_MANIFEST_PATH": "/etc/monolith/repo_docs_manifest.ndjson",
    },
    main = "//projects/monolith/app:main.py",
    repository = "ghcr.io/jomcgi/homelab/projects/monolith/backend",
    tars = [":repo_docs_manifest_layer"],
)

# Batch-jobs image: identical base/deps to :image, launching app/jobs_main.py.
# The dependency layers are byte-identical to the backend image, so this is
# nearly free to build and push (Bazel caches the layers; the registry dedupes
# the blobs). The runfiles/venv paths embed the binary name, so they read
# "jobs.runfiles" / ".jobs" here where the backend reads "main" / ".main".
py3_image(
    name = "jobs_image",
    binary = "//projects/monolith:jobs",
    env = {
        "PYTHONPATH": "/projects/monolith/jobs.runfiles/_main:/projects/monolith/jobs.runfiles/_main/projects/monolith",
        # worldcup-sim makes outbound HTTPS to worldcup26.ir; point OpenSSL at
        # certifi's bundle since the minimal base image has no CA certificates.
        "SSL_CERT_FILE": "/projects/monolith/jobs.runfiles/_main/projects/monolith/.jobs/lib/python3.13/site-packages/certifi/cacert.pem",
        "REPO_DOCS_MANIFEST_PATH": "/etc/monolith/repo_docs_manifest.ndjson",
    },
    main = "//projects/monolith/app:jobs_main.py",
    repository = "ghcr.io/jomcgi/homelab/projects/monolith/jobs",
    tars = [":repo_docs_manifest_layer"],
)

# Progress-ingest image (ADR agents/051): identical base/deps to :image,
# launching app/progress_main.py. Runs as a second container in the API pod on
# its own port 8091 so the EmberVM egress allowlist entry exposes exactly one
# write-only route. No claude CLI tar and no SSL cert env: the listener makes
# no outbound calls at all, it only accepts pushes and writes one column.
py3_image(
    name = "progress_image",
    binary = "//projects/monolith:progress",
    env = {
        "PYTHONPATH": "/projects/monolith/progress.runfiles/_main:/projects/monolith/progress.runfiles/_main/projects/monolith",
    },
    main = "//projects/monolith/app:progress_main.py",
    repository = "ghcr.io/jomcgi/homelab/projects/monolith/progress",
)

# Agents-tier image: the pruned authenticated MCP binary on its dedicated port.
# Its runfiles path follows the binary target name, as progress_image does.
py3_image(
    name = "main_agents_image",
    binary = "//projects/monolith:main_agents",
    env = {
        "PYTHONPATH": "/projects/monolith/main_agents.runfiles/_main:/projects/monolith/main_agents.runfiles/_main/projects/monolith",
    },
    main = "//projects/monolith/app:agents_main.py",
    repository = "ghcr.io/jomcgi/homelab/projects/monolith/agents",
)

# ---------------------------------------------------------------------------
# Public service (ADR 004 Phase 5): a pruned, read-only binary/image that runs
# app/main_public.py. It deliberately excludes every private write-path domain
# (agent/chat/scheduler/trips), the private entrypoints (app/main.py,
# app/mcp_app.py), the private knowledge routers/write internals, and the
# private home/observability writer paths. Those modules are
# PROVEN absent from app.main_public's runtime closure by
# app/main_public_imports_test.py, so excluding their FILES cannot break the
# public binary. Everything else (knowledge read API/store/models/public
# routers, home read router/config/models, ships/stars/hikes/dr_jobs/shared)
# stays in full. The pip deps mirror :monolith_backend wholesale: a missing
# wheel would crash the public binary at startup, so trimming is a follow-up.
# ---------------------------------------------------------------------------
# Agent service (issue #5656 slice 1): a pruned, authenticated MCP binary that
# runs app/agents_main.py. It depends only on the knowledge catalogue plus the
# auth, core, framework, shared, and app support code needed to serve it. Every
# unrelated domain and every private knowledge, home, cluster, and app target is
# absent from the dependency list below. app/main_agents_imports_test.py imports
# the entrypoint in a fresh process and PROVES that none of those private
# modules enter its runtime closure, so excluding their FILES is safe: any new
# eager dependency fails in CI before a later image can fail at startup. The
# pip dependencies mirror :monolith_backend wholesale for now. Trimming wheels
# is separate from enforcing the Python source boundary.
py_library(
    name = "monolith_agents_backend",
    visibility = ["//:__subpackages__"],
    deps = [
        "//projects/monolith/app:agents",
        "//projects/monolith/auth:agents",
        "//projects/monolith/chat:agents",
        "//projects/monolith/core:agents",
        "//projects/monolith/framework:agents",
        "//projects/monolith/knowledge:agents",
        "//projects/monolith/shared:agents",
        "@pip//astral",
        "@pip//beautifulsoup4",
        "@pip//boto3",
        "@pip//certifi",
        "@pip//discord_py",
        "@pip//fastapi",
        "@pip//fastmcp",
        "@pip//httpx",
        "@pip//icalendar",
        "@pip//kubernetes_asyncio",
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
        "@pip//pydantic_ai_slim",
        "@pip//pyjwt",
        "@pip//python_multipart",
        "@pip//pyyaml",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
        "@pip//starlette",
        "@pip//timelength",
        "@pip//trafilatura",
        "@pip//tzdata",
        "@pip//uvicorn",
        "@pip//websockets",
        "@pip//youtube_transcript_api",
    ],
)

py_venv_binary(
    name = "main_agents",
    srcs = ["//projects/monolith/app:agents_main.py"],  # keep
    imports = ["."],  # keep
    main = "//projects/monolith/app:agents_main.py",
    visibility = ["//:__subpackages__"],
    deps = [":monolith_agents_backend"],  # keep
)

alias(
    name = "monolith_public_backend",
    actual = "//projects/monolith/public_backend:monolith_public_backend",
    visibility = ["//:__subpackages__"],
)

py_venv_binary(
    name = "main_public",
    srcs = ["//projects/monolith/app:main_public.py"],  # keep
    imports = ["."],  # keep
    main = "//projects/monolith/app:main_public.py",
    visibility = ["//:__subpackages__"],
    deps = ["//projects/monolith/public_backend:monolith_public_backend"],  # keep
)

py3_image(
    name = "image_public",
    binary = "//projects/monolith:main_public",
    env = {
        # py3_image sets PYTHONPATH to the workspace root (_main/) but this
        # project's sibling packages (app/, knowledge/) need the package directory
        # on the path too, matching the imports = ["."] used by py_venv_binary.
        "PYTHONPATH": "/projects/monolith/main_public.runfiles/_main:/projects/monolith/main_public.runfiles/_main/projects/monolith",
        # The minimal ubuntu base image has no CA certificates; point OpenSSL
        # at certifi's bundle so outbound HTTPS works.
        "SSL_CERT_FILE": "/projects/monolith/main_public.runfiles/_main/projects/monolith/.main_public/lib/python3.13/site-packages/certifi/cacert.pem",
    },
    main = "//projects/monolith/app:main_public.py",
    repository = "ghcr.io/jomcgi/homelab/projects/monolith/public-backend",
)

# ---------------------------------------------------------------------------
# FastMonolith per-domain composition (ADR services/010): one shared binary
# that composes a single domain selected by MONOLITH_DOMAIN, and one build-only
# dual-arch image per domain baking that env in. All 18 images share ONE layer
# set (each extra image costs only its config); note the layers do NOT dedupe
# against :image in the registry, because py_image_layer embeds the binary
# name (main vs main_domain) in every path.
# Build one:  bazel build //projects/monolith:image_domain_hikes
# Build all:  bazel build //projects/monolith:domain_images
# ---------------------------------------------------------------------------
py_venv_binary(
    name = "main_domain",
    srcs = ["//projects/monolith/app:main_domain.py"],  # keep
    data = [
        # Same runtime data files as :main (any domain may be selected at
        # startup); only the static frontend bundle is omitted, because the
        # domain profile never serves it (framework: static_frontend=False).
        "//projects/monolith/campsites:catalog.json",
        "//projects/monolith/chat:directive_seed.md",
        "//projects/monolith/chat:orchestrator_bundle.md",
        "//projects/monolith/worldcup:ratings/elo_2026.json",
        "//projects/monolith/demos:loadtest_samples",
    ],
    imports = ["."],  # keep
    main = "//projects/monolith/app:main_domain.py",
    visibility = ["//:__subpackages__"],
    deps = [":monolith_backend"],  # keep
)

monolith_domain_images(
    name = "domain_images",
    binary = ":main_domain",
    config_test_domain = "hikes",
    domains = MONOLITH_DOMAINS,
    extra_env = {"REPO_DOCS_MANIFEST_PATH": "/etc/monolith/repo_docs_manifest.ndjson"},
    extra_tars = [":repo_docs_manifest_layer"],
    main = "//projects/monolith/app:main_domain.py",
)

py_test(
    name = "auth_verifier_test",
    srcs = ["//projects/monolith/auth:verifier_test.py"],
    imports = ["."],
    deps = [
        ":pkg_auth",
        "@pip//cryptography",
        # PyJWT is currently locked transitively through mcp and semgrep.
        "@pip//pyjwt",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "auth_plumbing_test",
    srcs = ["//projects/monolith/auth:plumbing_test.py"],
    imports = ["."],
    deps = [
        ":pkg_auth",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "markers_test",
    srcs = ["//projects/monolith/shared:testing/markers_test.py"],
    imports = ["."],
    deps = [
        ":shared_testing",
        "@pip//pytest",
    ],
)

py_test(
    name = "package_classification_test",
    srcs = ["package_classification_test.py"],
    data = [
        "BUILD",
        +"//projects/monolith/agent:BUILD",
        "//projects/monolith/agent_sessions:BUILD",
        "//projects/monolith/app:BUILD",
        "//projects/monolith/artifact:BUILD",
        "//projects/monolith/auth:BUILD",
        "//projects/monolith/campsites:BUILD",
        "//projects/monolith/chat:BUILD",
        "//projects/monolith/chat_public:BUILD",
        "//projects/monolith/cluster:BUILD",
        "//projects/monolith/core:BUILD",
        "//projects/monolith/demos:BUILD",
        "//projects/monolith/dr_jobs:BUILD",
        "//projects/monolith/e2e:BUILD",
        "//projects/monolith/ember_public:BUILD",
        "//projects/monolith/faas:BUILD",
        "//projects/monolith/framework:BUILD",
        "//projects/monolith/goosecracker:BUILD",
        "//projects/monolith/grimoire:BUILD",
        "//projects/monolith/grimoire_chat:BUILD",
        "//projects/monolith/hikes:BUILD",
        "//projects/monolith/home:BUILD",
        "//projects/monolith/knowledge:BUILD",
        "//projects/monolith/moving:BUILD",
        "//projects/monolith/observability:BUILD",
        "//projects/monolith/public_backend:BUILD",
        "//projects/monolith/sandbox:BUILD",
        "//projects/monolith/scheduler:BUILD",
        "//projects/monolith/scripts:BUILD",
        "//projects/monolith/semgrep_scan:BUILD",
        "//projects/monolith/shared:BUILD",
        "//projects/monolith/ships:BUILD",
        "//projects/monolith/shotter:BUILD",
        "//projects/monolith/stars:BUILD",
        "//projects/monolith/swarm:BUILD",
        "//projects/monolith/trips:BUILD",
        "//projects/monolith/updates:BUILD",
        "//projects/monolith/worldcup:BUILD",
    ],
    deps = ["@pip//pytest"],
)

# Reads every domain source file from disk (via rglob) to enforce that domains
# only import each other through <domain>/api.py, so the scanned files must be
# materialised as data (the same pattern as bdd_completeness_test).
py_test(
    name = "import_boundaries_test",
    srcs = ["import_boundaries_test.py"],
    data = [
        "//projects/monolith/agent:boundary_sources",
        "//projects/monolith/agent_sessions:boundary_sources",
        "//projects/monolith/artifact:boundary_sources",
        "//projects/monolith/auth:boundary_sources",
        "//projects/monolith/campsites:boundary_sources",
        "//projects/monolith/chat:boundary_sources",
        "//projects/monolith/chat_public:boundary_sources",
        "//projects/monolith/dr_jobs:boundary_sources",
        "//projects/monolith/goosecracker:boundary_sources",
        "//projects/monolith/grimoire_chat:boundary_sources",
        "//projects/monolith/hikes:boundary_sources",
        "//projects/monolith/home:boundary_sources",
        "//projects/monolith/knowledge:boundary_sources",
        "//projects/monolith/moving:boundary_sources",
        "//projects/monolith/scheduler:boundary_sources",
        "//projects/monolith/ships:boundary_sources",
        "//projects/monolith/stars:boundary_sources",
        "//projects/monolith/trips:boundary_sources",
        "//projects/monolith/worldcup:boundary_sources",
    ],
    imports = ["."],
    deps = ["@pip//pytest"],
)

py_test(
    name = "view_test",
    srcs = ["//projects/monolith/swarm:view_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "rows_test",
    srcs = ["//projects/monolith/swarm:rows_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "models_test",
    srcs = ["//projects/monolith/swarm:models_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_turn_artifact_test",
    srcs = ["//projects/monolith/swarm:turn_artifact_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_graph_test",
    srcs = ["//projects/monolith/swarm:graph_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_store_test",
    srcs = ["//projects/monolith/swarm:store_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "rationale_test",
    srcs = ["//projects/monolith/swarm:rationale_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        ":swarm_rationale",
        "@pip//pytest",
    ],
)

py_test(
    name = "deviations_test",
    srcs = ["//projects/monolith/swarm:deviations_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "budget_test",
    srcs = ["//projects/monolith/swarm:budget_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "tier_test",
    srcs = ["//projects/monolith/swarm:tier_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "frontend_test",
    srcs = ["//projects/monolith/app:frontend_test.py"],
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
    srcs = ["//projects/monolith/home:schedule_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//httpx",
        "@pip//icalendar",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//tzdata",
    ],
)

py_test(
    name = "home_schedule_extra_test",
    srcs = ["//projects/monolith/home:schedule_extra_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//httpx",
        "@pip//icalendar",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//tzdata",
    ],
)

py_test(
    name = "home_schedule_router_test",
    srcs = ["//projects/monolith/home:schedule_router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        ":pkg_framework",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//tzdata",
    ],
)

py_test(
    name = "home_dashboard_test",
    srcs = ["//projects/monolith/home:dashboard_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "//projects/monolith/public_backend:monolith_public_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
        "@pip//tzdata",
    ],
)

py_test(
    name = "home_cluster_snapshot_test",
    srcs = ["//projects/monolith/home:cluster_snapshot_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlalchemy",
    ],
)

py_test(
    name = "db_test",
    srcs = ["//projects/monolith/core:db_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "github_test",
    srcs = ["//projects/monolith/core:github_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "mcp_policy_test",
    srcs = ["//projects/monolith/core:mcp_policy_test.py"],
    imports = ["."],
    deps = [
        ":pkg_auth",
        ":pkg_core",
        "@pip//fastmcp",
        "@pip//pytest",
    ],
)

py_test(
    name = "observability_merged_prs_test",
    srcs = ["//projects/monolith/observability:merged_prs_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "observability_public_router_test",
    srcs = ["//projects/monolith/observability:public_router_test.py"],
    imports = ["."],
    deps = [
        "//projects/monolith/public_backend:monolith_public_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

# The sources are `data`, not just `srcs`: this test WALKS the tree rather than
# importing it, and a bazel test only sees its own runfiles. Without the glob the
# walk finds nothing, the violations list is empty, and the test passes while
# guarding nothing. The test asserts a floor on the file count for the same
# reason, so a future glob that silently stops matching fails loudly.
py_test(
    name = "github_repo_slug_test",
    srcs = ["github_repo_slug_test.py"],
    data = glob(
        ["*.py"],
        exclude = [
            "*_test.py",
            "test_*.py",
            "conftest.py",
        ],
    ) + [
        "//projects/monolith/agent:source_scan_files",
        "//projects/monolith/agent_sessions:source_scan_files",
        "//projects/monolith/app:source_scan_files",
        "//projects/monolith/artifact:source_scan_files",
        "//projects/monolith/auth:source_scan_files",
        "//projects/monolith/campsites:source_scan_files",
        "//projects/monolith/chat:source_scan_files",
        "//projects/monolith/chat_public:source_scan_files",
        "//projects/monolith/cluster:source_scan_files",
        "//projects/monolith/core:source_scan_files",
        "//projects/monolith/demos:source_scan_files",
        "//projects/monolith/dr_jobs:source_scan_files",
        "//projects/monolith/e2e:source_scan_files",
        "//projects/monolith/ember_public:source_scan_files",
        "//projects/monolith/faas:source_scan_files",
        "//projects/monolith/framework:source_scan_files",
        "//projects/monolith/goosecracker:source_scan_files",
        "//projects/monolith/grimoire:source_scan_files",
        "//projects/monolith/grimoire_chat:source_scan_files",
        "//projects/monolith/hikes:source_scan_files",
        "//projects/monolith/home:source_scan_files",
        "//projects/monolith/knowledge:source_scan_files",
        "//projects/monolith/moving:source_scan_files",
        "//projects/monolith/observability:source_scan_files",
        "//projects/monolith/sandbox:source_scan_files",
        "//projects/monolith/scheduler:source_scan_files",
        "//projects/monolith/scripts:source_scan_files",
        "//projects/monolith/semgrep_scan:source_scan_files",
        "//projects/monolith/shared:source_scan_files",
        "//projects/monolith/ships:source_scan_files",
        "//projects/monolith/shotter:source_scan_files",
        "//projects/monolith/stars:source_scan_files",
        "//projects/monolith/swarm:source_scan_files",
        "//projects/monolith/trips:source_scan_files",
        "//projects/monolith/updates:source_scan_files",
        "//projects/monolith/worldcup:source_scan_files",
    ],
    imports = ["."],
)

py_test(
    name = "jobs_main_test",
    srcs = ["//projects/monolith/app:jobs_main_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//sqlmodel",
        "@pip//typer",
    ],
)

py_test(
    name = "main_test",
    srcs = ["//projects/monolith/app:main_test.py"],
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
    name = "main_public_routes_test",
    srcs = ["//projects/monolith/app:main_public_routes_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//fastapi",
        "@pip//pytest",
        "@pip//sqlmodel",
        "@pip//tzdata",
    ],
)

py_test(
    name = "main_public_imports_test",
    srcs = ["//projects/monolith/app:main_public_imports_test.py"],
    imports = ["."],
    # Depend on the PRUNED public library, not :monolith_backend. This makes the
    # test a pre-deploy validator of the prune itself: if the public glob omits a
    # file that app.main_public actually needs, the child import fails
    # (returncode != 0) here in CI rather than only at pod startup. It still
    # asserts the forbidden private modules are absent from the closure.
    deps = [
        "//projects/monolith/public_backend:monolith_public_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "main_agents_imports_test",
    srcs = ["//projects/monolith/app:main_agents_imports_test.py"],
    imports = ["."],
    # Depend on the PRUNED agents library, not :monolith_backend. This makes
    # the test validate both the source prune and the fresh-process import
    # closure before any later image is assembled.
    deps = [
        ":monolith_agents_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "framework_core_test",
    srcs = ["//projects/monolith/framework:core_test.py"],
    imports = ["."],
    deps = [
        ":pkg_framework",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "main_domain_test",
    srcs = ["//projects/monolith/app:main_domain_test.py"],
    data = ["domain_images.bzl"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//fastapi",
        "@pip//pytest",
        "@pip//sqlmodel",
        "@pip//tzdata",
    ],
)

py_test(
    name = "cluster_summarize_test",
    srcs = ["//projects/monolith/cluster:summarize_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cluster",
        "@pip//pytest",
    ],
)

py_test(
    name = "cluster_mcp_test",
    srcs = ["//projects/monolith/cluster:mcp_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cluster",
        ":pkg_core",
        "@pip//fastmcp",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "semgrep_mcp_test",
    srcs = ["//projects/monolith/semgrep_scan:mcp_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_semgrep_scan",
        "@pip//fastmcp",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "semgrep_report_test",
    srcs = ["//projects/monolith/semgrep_scan:report_test.py"],
    data = [
        # report_test loads this captured cli_output fixture at runtime via
        # Path(__file__).parent/"testdata"; the "semgrep_scan/**/*.py" backend
        # glob does not capture the .json, so ship it into runfiles. 8 results
        # (7 findings + 1 ignored), derived from a real 1.168.0 Pro finding.
        "//projects/monolith/semgrep_scan:testdata/real_cli_output.json",
    ],
    imports = ["."],
    # Drives the async relay via asyncio.run (sync test fns), so no
    # pytest-asyncio plugin is needed.
    deps = [
        ":pkg_semgrep_scan",
        "@pip//pytest",
        # httpx: the test mocks semgrep_scan.report.httpx.post to capture the three
        # App uploads (CREATE + /results + /complete) and their Bearer auth.
        "@pip//httpx",
        # pysemgrep out.* atd types (light). Pinned to 1.168.0 in
        # bazel/requirements/tools.in to match the fc-invoke Pro engine that
        # produced the fixture. NOT ScanHandler / app_session.
        "@pip//semgrep",
    ],
)

py_test(
    name = "semgrep_router_test",
    srcs = ["//projects/monolith/semgrep_scan:router_test.py"],
    imports = ["."],
    # gazelle:exclude semgrep_scan means this py_test is hand-added, not generated.
    # Drives the async webhook via FastAPI TestClient (BackgroundTasks run inline
    # after the response), mocking scan_files / report_pr_scan / httpx so no live
    # GitHub, fc-invoke, or Semgrep App is touched.
    deps = [
        ":pkg_semgrep_scan",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "semgrep_perf_store_test",
    srcs = ["//projects/monolith/semgrep_scan:perf_store_test.py"],
    imports = ["."],
    # gazelle:exclude semgrep_scan means this py_test is hand-added, not generated.
    deps = [
        ":pkg_semgrep_scan",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "semgrep_perf_harvest_test",
    srcs = ["//projects/monolith/semgrep_scan:perf_harvest_test.py"],
    imports = ["."],
    # gazelle:exclude semgrep_scan means this py_test is hand-added, not generated.
    deps = [
        ":pkg_semgrep_scan",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "semgrep_cohorts_test",
    srcs = ["//projects/monolith/semgrep_scan:cohorts_test.py"],
    imports = ["."],
    # gazelle:exclude semgrep_scan means this py_test is hand-added, not generated.
    deps = [
        ":pkg_semgrep_scan",
        "@pip//pytest",
    ],
)

py_test(
    name = "semgrep_perf_compare_test",
    srcs = ["//projects/monolith/semgrep_scan:perf_compare_test.py"],
    imports = ["."],
    # gazelle:exclude semgrep_scan means this py_test is hand-added, not generated.
    deps = [
        ":pkg_semgrep_scan",
        "@pip//pytest",
    ],
)

py_test(
    name = "semgrep_perf_router_test",
    srcs = ["//projects/monolith/semgrep_scan:perf_router_test.py"],
    imports = ["."],
    # gazelle:exclude semgrep_scan means this py_test is hand-added, not generated.
    deps = [
        ":pkg_core",
        ":pkg_semgrep_scan",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "semgrep_perf_webhook_test",
    srcs = ["//projects/monolith/semgrep_scan:perf_webhook_test.py"],
    imports = ["."],
    # gazelle:exclude semgrep_scan means this py_test is hand-added, not generated.
    deps = [
        ":pkg_semgrep_scan",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "sandbox_mcp_test",
    srcs = ["//projects/monolith/sandbox:mcp_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_sandbox",
        "@pip//fastmcp",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

# sandbox is gazelle-excluded, so its py_tests are hand-registered.
py_test(
    name = "sandbox_client_test",
    srcs = ["//projects/monolith/sandbox:client_test.py"],
    imports = ["."],
    deps = [
        ":pkg_sandbox",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

# EmberVM R4 scratch-postgres DSN plumbing (D-R4.PR-11.1): asserts the DSN is
# injected into submitted Python code (guest-side psycopg-connect), repr-escaped,
# and reaches the POSTed payload; and that other languages and an unset DSN are
# no-ops.
py_test(
    name = "sandbox_scratch_postgres_test",
    srcs = ["//projects/monolith/sandbox:scratch_postgres_test.py"],
    imports = ["."],
    deps = [
        ":pkg_sandbox",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "integration_test",
    size = "medium",
    srcs = ["//projects/monolith/app:integration_test.py"],
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
    srcs = ["//projects/monolith/chat:models_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_models_db_constraints_test",
    srcs = ["//projects/monolith/chat:models_db_constraints_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_models_reminder_test",
    srcs = ["//projects/monolith/chat:models_reminder_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_reminders_test",
    srcs = ["//projects/monolith/chat:reminders_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_jobs_reminder_test",
    srcs = ["//projects/monolith/chat:jobs_reminder_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_models_coverage_test",
    srcs = ["//projects/monolith/chat:models_coverage_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "shared_embedding_test",
    srcs = ["//projects/monolith/shared:embedding_test.py"],
    imports = ["."],
    deps = [
        ":pkg_shared",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "shared_pricing_test",
    srcs = ["//projects/monolith/shared:pricing_test.py"],
    imports = ["."],
    deps = [
        ":pkg_shared",
        "@pip//pytest",
    ],
)

py_test(
    name = "shared_inference_test",
    srcs = ["//projects/monolith/shared:inference_test.py"],
    imports = ["."],
    deps = [
        ":pkg_shared",
        "@pip//opentelemetry_sdk",
        "@pip//pytest",
    ],
)

py_test(
    name = "shared_k8s_auth_test",
    srcs = ["//projects/monolith/shared:k8s_auth_test.py"],
    imports = ["."],
    deps = [
        ":pkg_shared",
        "@pip//pytest",
    ],
)

py_test(
    name = "shared_embedding_retry_test",
    srcs = ["//projects/monolith/shared:embedding_retry_test.py"],
    imports = ["."],
    deps = [
        ":pkg_shared",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "shared_embedding_batch_test",
    srcs = ["//projects/monolith/shared:embedding_batch_test.py"],
    imports = ["."],
    deps = [
        ":pkg_shared",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_store_test",
    srcs = ["//projects/monolith/chat:store_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_query_stats_test",
    srcs = ["//projects/monolith/chat:query_stats_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_fetch_window_test",
    srcs = ["//projects/monolith/chat:store_fetch_window_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_digest_test",
    srcs = ["//projects/monolith/chat:digest_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_channel_data_test",
    srcs = ["//projects/monolith/chat:channel_data_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_observer_test",
    srcs = ["//projects/monolith/chat:observer_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_observer_job_test",
    srcs = ["//projects/monolith/chat:observer_job_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_outbox_test",
    srcs = ["//projects/monolith/chat:outbox_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_whatsapp_outbox_test",
    srcs = ["//projects/monolith/chat:whatsapp_outbox_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_whatsapp_group_test",
    srcs = ["//projects/monolith/chat:whatsapp_group_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_whatsapp_inbound_test",
    srcs = ["//projects/monolith/chat:whatsapp_inbound_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
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
    srcs = ["//projects/monolith/chat:whatsapp_timeparse_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_whatsapp_intents_test",
    srcs = ["//projects/monolith/chat:whatsapp_intents_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_whatsapp_calendar_test",
    srcs = ["//projects/monolith/chat:whatsapp_calendar_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_whatsapp_capabilities_test",
    srcs = ["//projects/monolith/chat:whatsapp_capabilities_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_whatsapp_digest_test",
    srcs = ["//projects/monolith/chat:whatsapp_digest_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_orchestrator_bundle_test",
    srcs = ["//projects/monolith/chat:orchestrator_bundle_test.py"],
    # The test reads both the hand-written base prompt and the committed
    # generated bundle directly from runfiles (Path(__file__).parent), so ship
    # them as data (mirrors chat_directives_test's directive_seed.md). The
    # generator itself is exercised via its pure functions only: the git
    # ls-files-driven path discovery has no .git to walk inside the bazel test
    # sandbox, same reason gen_repo_docs_manifest_test.py never calls
    # iter_doc_paths.
    data = [
        "//projects/monolith/chat:orchestrator_bundle.md",
        "//projects/monolith/chat:orchestrator_prompt.md",
    ],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_acl_test",
    srcs = ["//projects/monolith/chat:acl_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_attention_log_test",
    srcs = ["//projects/monolith/chat:attention_log_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_directives_test",
    srcs = ["//projects/monolith/chat:directives_test.py"],
    # directives.get_active reads the committed seed from runfiles via
    # Path(__file__).parent / "directive_seed.md", so ship it as test data
    # (mirrors campsites_client_test's catalog.json).
    data = ["//projects/monolith/chat:directive_seed.md"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_ambient_analysis_test",
    srcs = ["//projects/monolith/chat:ambient_analysis_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_autopilot_job_test",
    srcs = ["//projects/monolith/chat:autopilot_job_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_attention_test",
    srcs = ["//projects/monolith/chat:attention_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
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
    srcs = ["//projects/monolith/chat:reply_sanitize_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_web_search_test",
    srcs = ["//projects/monolith/chat:web_search_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_bot_test",
    srcs = ["//projects/monolith/chat:bot_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_bot_send_message_test",
    srcs = ["//projects/monolith/chat:bot_send_message_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_agent_test",
    srcs = ["//projects/monolith/chat:agent_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_coverage_test",
    srcs = ["//projects/monolith/chat:store_coverage_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_on_message_test",
    srcs = ["//projects/monolith/chat:bot_on_message_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        ":pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_streaming_test",
    srcs = ["//projects/monolith/chat:bot_streaming_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_web_search_coverage_test",
    srcs = ["//projects/monolith/chat:web_search_coverage_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "shared_embedding_coverage_test",
    srcs = ["//projects/monolith/shared:embedding_coverage_test.py"],
    imports = ["."],
    deps = [
        ":pkg_shared",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_agent_deps_test",
    srcs = ["//projects/monolith/chat:agent_deps_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_agent_coverage_test",
    srcs = ["//projects/monolith/chat:agent_coverage_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "main_lifespan_discord_test",
    srcs = ["//projects/monolith/app:main_lifespan_discord_test.py"],
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
    name = "leadership_test",
    srcs = ["//projects/monolith/core:leadership_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_agent_extra_test",
    srcs = ["//projects/monolith/chat:agent_extra_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_extra_test",
    srcs = ["//projects/monolith/chat:bot_extra_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_web_search_extra_test",
    srcs = ["//projects/monolith/chat:web_search_extra_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_store_extra_test",
    srcs = ["//projects/monolith/chat:store_extra_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "main_otel_test",
    srcs = ["//projects/monolith/app:main_otel_test.py"],
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
    srcs = ["//projects/monolith/shared:embedding_extra_test.py"],
    imports = ["."],
    deps = [
        ":pkg_shared",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_changelog_test",
    srcs = ["//projects/monolith/chat:changelog_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_changelog_fetch_test",
    srcs = ["//projects/monolith/chat:changelog_fetch_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_summarizer_test",
    srcs = ["//projects/monolith/chat:summarizer_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "main_extra_test",
    srcs = ["//projects/monolith/app:main_extra_test.py"],
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
    srcs = ["//projects/monolith/core:log_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        "@pip//pytest",
    ],
)

py_test(
    name = "log_config_test",
    srcs = ["//projects/monolith/core:log_config_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        "@pip//pytest",
    ],
)

py_test(
    name = "main_log_task_test",
    srcs = ["//projects/monolith/app:main_log_task_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "main_sidecar_test",
    srcs = ["//projects/monolith/app:main_sidecar_test.py"],
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
    name = "store_integrity_test",
    srcs = ["//projects/monolith/chat:store_integrity_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "bot_self_message_test",
    srcs = ["//projects/monolith/chat:bot_self_message_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "bot_embed_test",
    srcs = ["//projects/monolith/chat:bot_embed_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "web_search_headers_test",
    srcs = ["//projects/monolith/chat:web_search_headers_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_models_summary_test",
    srcs = ["//projects/monolith/chat:models_summary_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_channel_summary_test",
    srcs = ["//projects/monolith/chat:store_channel_summary_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_summary_test",
    srcs = ["//projects/monolith/chat:store_summary_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_vision_errors_test",
    srcs = ["//projects/monolith/chat:vision_errors_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_orchestrator_client_test",
    srcs = ["//projects/monolith/chat:orchestrator_client_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_orchestrator_plan_test",
    srcs = ["//projects/monolith/chat:orchestrator_plan_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_orchestrator_test",
    srcs = ["//projects/monolith/chat:orchestrator_test.py"],
    # compile() reads the committed bundle from runfiles (Path(__file__).parent),
    # so ship it as data (mirrors chat_orchestrator_bundle_test).
    data = ["//projects/monolith/chat:orchestrator_bundle.md"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_summarizer_llm_caller_test",
    srcs = ["//projects/monolith/chat:summarizer_llm_caller_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_summarizer_agent_reply_test",
    srcs = ["//projects/monolith/chat:summarizer_agent_reply_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_agent_prompt_echo_test",
    srcs = ["//projects/monolith/chat:bot_agent_prompt_echo_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        ":pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_summarizer_channel_test",
    srcs = ["//projects/monolith/chat:summarizer_channel_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_exception_test",
    srcs = ["//projects/monolith/chat:bot_exception_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_bot_error_handling_test",
    srcs = ["//projects/monolith/chat:bot_error_handling_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_agent_tools_test",
    srcs = ["//projects/monolith/chat:agent_tools_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_store_attachments_test",
    srcs = ["//projects/monolith/chat:store_attachments_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_agent_coerce_test",
    srcs = ["//projects/monolith/chat:agent_coerce_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_store_blob_test",
    srcs = ["//projects/monolith/chat:store_blob_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_flush_blob_test",
    srcs = ["//projects/monolith/chat:store_flush_blob_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_save_messages_test",
    srcs = ["//projects/monolith/chat:store_save_messages_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_embed_text_test",
    srcs = ["//projects/monolith/chat:store_embed_text_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_agent_tool_execution_test",
    srcs = ["//projects/monolith/chat:agent_tool_execution_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_agent_channel_digest_tool_test",
    srcs = ["//projects/monolith/chat:agent_channel_digest_tool_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_agent_reminder_tools_test",
    srcs = ["//projects/monolith/chat:agent_reminder_tools_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_attachments_test",
    srcs = ["//projects/monolith/chat:bot_attachments_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_bot_multimodal_test",
    srcs = ["//projects/monolith/chat:bot_multimodal_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_startup_test",
    srcs = ["//projects/monolith/chat:startup_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        ":pkg_scheduler",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_summarizer_startup_test",
    srcs = ["//projects/monolith/chat:summarizer_startup_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        ":pkg_scheduler",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_summarizer_prompts_test",
    srcs = ["//projects/monolith/chat:summarizer_prompts_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_vision_timeout_test",
    srcs = ["//projects/monolith/chat:vision_timeout_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_web_search_null_results_test",
    srcs = ["//projects/monolith/chat:web_search_null_results_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_models_persistence_test",
    srcs = ["//projects/monolith/chat:models_persistence_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_upsert_test",
    srcs = ["//projects/monolith/chat:store_upsert_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_bulk_test",
    srcs = ["//projects/monolith/chat:store_bulk_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_models_validation_test",
    srcs = ["//projects/monolith/chat:models_validation_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "app_main_summary_test",
    srcs = ["//projects/monolith/app:main_summary_test.py"],
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
    srcs = ["//projects/monolith/app:main_summary_edge_test.py"],
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
    name = "scripts_preview_layout_test",
    srcs = ["//projects/monolith/scripts:preview_layout_test.py"],
    data = ["//projects/monolith/scripts:preview-layout.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//networkx",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_agent_helper_functions_test",
    srcs = ["//projects/monolith/chat:agent_helper_functions_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_backfill_test",
    srcs = ["//projects/monolith/chat:backfill_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_vision_test",
    srcs = ["//projects/monolith/chat:vision_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_new_coverage_test",
    srcs = ["//projects/monolith/chat:chat_new_coverage_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
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
    srcs = ["//projects/monolith/chat:web_search_missing_fields_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_store_non_integrity_error_test",
    srcs = ["//projects/monolith/chat:store_non_integrity_error_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_summarizer_none_id_test",
    srcs = ["//projects/monolith/chat:summarizer_none_id_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "shared_embedding_null_test",
    srcs = ["//projects/monolith/shared:embedding_null_test.py"],
    imports = ["."],
    deps = [
        ":pkg_shared",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_bot_session_failure_test",
    srcs = ["//projects/monolith/chat:bot_session_failure_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_bot_thinking_test",
    srcs = ["//projects/monolith/chat:bot_thinking_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_bot_embeddable_content_test",
    srcs = ["//projects/monolith/chat:bot_embeddable_content_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_agent_max_tokens_test",
    srcs = ["//projects/monolith/chat:agent_max_tokens_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_agent_signpost_test",
    srcs = ["//projects/monolith/chat:agent_signpost_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "shared_embedding_retryable_test",
    srcs = ["//projects/monolith/shared:embedding_retryable_test.py"],
    imports = ["."],
    deps = [
        ":pkg_shared",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_vision_retryable_test",
    srcs = ["//projects/monolith/chat:vision_retryable_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_store_null_data_test",
    srcs = ["//projects/monolith/chat:store_null_data_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_auto_search_test",
    srcs = ["//projects/monolith/chat:bot_auto_search_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_bot_response_storage_test",
    srcs = ["//projects/monolith/chat:bot_response_storage_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "shared_embedding_connect_timeout_test",
    srcs = ["//projects/monolith/shared:embedding_connect_timeout_test.py"],
    imports = ["."],
    deps = [
        ":pkg_shared",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_agent_web_search_tool_test",
    srcs = ["//projects/monolith/chat:agent_web_search_tool_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_bot_summary_injection_test",
    srcs = ["//projects/monolith/chat:bot_summary_injection_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_identity_note_test",
    srcs = ["//projects/monolith/chat:bot_identity_note_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_subject_note_test",
    srcs = ["//projects/monolith/chat:bot_subject_note_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_attachment_download_test",
    srcs = ["//projects/monolith/chat:bot_attachment_download_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
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
        "//projects/monolith/e2e:__init__.py",
        "//projects/monolith/e2e:conftest.py",
        "//projects/monolith/e2e:e2e_test.py",
    ],
    data = [
        "//projects/monolith/chart:migrations",
        "@postgres_test//:postgres",
    ],
    env = {"PYTEST_ADDOPTS": "-p shared.testing.plugin"},
    imports = ["."],
    tags = ["e2e"],
    deps = [
        ":pkg_cyclegroup",
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
        "//projects/monolith/e2e:__init__.py",
        "//projects/monolith/e2e:conftest.py",
        "//projects/monolith/e2e:e2e_playwright_test.py",
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
        ":pkg_knowledge",
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
    srcs = ["//projects/monolith/chat:router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "agent_sessions_router_test",
    srcs = ["//projects/monolith/agent_sessions:router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        ":pkg_core",
        # The factory decision route hands the work to swarm.factory_decisions.
        ":pkg_swarm",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_config_test",
    srcs = ["//projects/monolith/swarm:config_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_runtime_test",
    srcs = ["//projects/monolith/swarm:runtime_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//dbos",
        "@pip//fastapi",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_policy_test",
    srcs = ["//projects/monolith/swarm:policy_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_classifier_test",
    srcs = ["//projects/monolith/swarm:classifier_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_factory_conductor_test",
    srcs = ["//projects/monolith/swarm:factory_conductor_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        ":pkg_auth",
        ":pkg_swarm",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_model_pool_test",
    srcs = ["//projects/monolith/swarm:model_pool_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        ":pkg_swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_factory_controls_test",
    srcs = ["//projects/monolith/swarm:factory_controls_test.py"],
    imports = ["."],
    deps = [
        ":pkg_auth",
        ":pkg_swarm",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_mcp_test",
    srcs = ["//projects/monolith/swarm:mcp_test.py"],
    imports = ["."],
    deps = [
        ":pkg_auth",
        ":pkg_swarm",
        "@pip//fastmcp",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "swarm_factory_intake_test",
    srcs = ["//projects/monolith/swarm:factory_intake_test.py"],
    imports = ["."],
    deps = [
        ":pkg_auth",
        ":pkg_swarm",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_factory_intake_loop_test",
    srcs = ["//projects/monolith/swarm:factory_intake_loop_test.py"],
    imports = ["."],
    deps = [
        ":pkg_auth",
        ":pkg_swarm",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_factory_landing_test",
    srcs = ["//projects/monolith/swarm:factory_landing_test.py"],
    imports = ["."],
    deps = [
        ":pkg_auth",
        ":pkg_swarm",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_factory_quota_guard_test",
    srcs = ["//projects/monolith/swarm:factory_quota_guard_test.py"],
    imports = ["."],
    deps = [
        ":pkg_auth",
        ":pkg_swarm",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_factory_refine_test",
    srcs = ["//projects/monolith/swarm:factory_refine_test.py"],
    imports = ["."],
    deps = [
        ":pkg_auth",
        ":pkg_swarm",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_factory_decisions_test",
    srcs = ["//projects/monolith/swarm:factory_decisions_test.py"],
    imports = ["."],
    deps = [
        ":pkg_auth",
        ":pkg_swarm",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_factory_escalation_test",
    srcs = ["//projects/monolith/swarm:factory_escalation_test.py"],
    imports = ["."],
    deps = [
        ":pkg_auth",
        ":pkg_swarm",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_factory_fence_test",
    srcs = ["//projects/monolith/swarm:factory_fence_test.py"],
    imports = ["."],
    deps = [
        ":pkg_auth",
        ":pkg_swarm",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_factory_router_test",
    srcs = ["//projects/monolith/swarm:factory_router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_auth",
        ":pkg_swarm",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_factory_integration_test",
    srcs = ["//projects/monolith/swarm:factory_integration_test.py"],
    imports = ["."],
    deps = [
        ":pkg_auth",
        ":pkg_swarm",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_factory_supervision_test",
    srcs = ["//projects/monolith/swarm:factory_supervision_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        ":pkg_auth",
        ":pkg_swarm",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_factory_attempt_stop_test",
    srcs = ["//projects/monolith/swarm:factory_attempt_stop_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        ":pkg_auth",
        ":pkg_core",
        ":pkg_cyclegroup",
        ":pkg_swarm",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_node_workflows_test",
    srcs = ["//projects/monolith/swarm:node_workflows_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        ":pkg_auth",
        ":pkg_swarm",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_factory_transport_test",
    srcs = ["//projects/monolith/agent_sessions:factory_transport_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_router_test",
    srcs = ["//projects/monolith/swarm:router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_drainer_router_test",
    srcs = ["//projects/monolith/swarm:drainer_router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        ":pkg_cyclegroup",
        ":pkg_swarm",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_health_test",
    srcs = ["//projects/monolith/swarm:health_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        ":pkg_swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_drainer_test",
    srcs = ["//projects/monolith/swarm:drainer_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        ":pkg_cyclegroup",
        ":pkg_knowledge",
        ":pkg_swarm",
        "@pip//opentelemetry_sdk",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_extraction_test",
    srcs = ["//projects/monolith/knowledge:extraction_test.py"],
    data = ["//projects/monolith/knowledge:testdata/codex_session_redacted.txt"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pydantic",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_entities_test",
    srcs = ["//projects/monolith/knowledge:entities_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_extraction_gates_test",
    srcs = ["//projects/monolith/knowledge:extraction_gates_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_recall_test",
    srcs = ["//projects/monolith/knowledge:recall_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        ":pkg_knowledge",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_gate_calibration_test",
    srcs = ["//projects/monolith/knowledge:gate_calibration_test.py"],
    data = [
        "//projects/monolith/knowledge:testdata/atoms-all.tsv",
        "//projects/monolith/knowledge:testdata/atoms-graded.tsv",
    ],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_lens_test",
    srcs = ["//projects/monolith/knowledge:lens_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_docfix_test",
    srcs = ["//projects/monolith/knowledge:docfix_test.py"],
    data = [
        "//projects/monolith/chart:docfix_review_test_data",
        "//projects/monolith/deploy:deploy_values",
        "@multitool//tools/helm",
    ],
    env = {
        "DEPLOY_VALUES": "$(rootpath //projects/monolith/deploy:deploy_values)",
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
    },
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        ":pkg_swarm",
        "@pip//opentelemetry_sdk",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_extraction_repo_diff_test",
    srcs = ["//projects/monolith/knowledge:extraction_repo_diff_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_health_test",
    srcs = ["//projects/monolith/knowledge:health_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_burst_test",
    srcs = ["//projects/monolith/knowledge:burst_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_drain_console_test",
    srcs = ["//projects/monolith/swarm:drain_console_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_drain_console_router_test",
    srcs = ["//projects/monolith/swarm:drain_console_router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_compare_router_test",
    srcs = ["//projects/monolith/swarm:compare_router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_unified_diff_test",
    srcs = ["//projects/monolith/swarm:unified_diff_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_walkthrough_composer_test",
    srcs = ["//projects/monolith/swarm:walkthrough_composer_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_workflows_test",
    srcs = ["//projects/monolith/swarm:workflows_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_steps_test",
    srcs = ["//projects/monolith/swarm:steps_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "agent_sessions_permit_supervision_test",
    srcs = ["//projects/monolith/agent_sessions:permit_supervision_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_factory_view_test",
    srcs = ["//projects/monolith/agent_sessions:factory_view_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

# Pure shaping for the public factory snapshot: every helper takes plain dicts,
# so this links no database and no swarm package.
py_test(
    name = "agent_sessions_factory_public_test",
    srcs = ["//projects/monolith/agent_sessions:factory_public_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_admission_test",
    srcs = ["//projects/monolith/agent_sessions:admission_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_store_test",
    srcs = ["//projects/monolith/agent_sessions:store_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

bdd_test(
    name = "agent_sessions_store_postgres_test",
    srcs = ["//projects/monolith/agent_sessions:store_postgres_test.py"],
)

bdd_test(
    name = "agent_sessions_process_crash_test",
    srcs = [
        "//projects/monolith/agent_sessions:process_crash_driver_test.py",
        "//projects/monolith/agent_sessions:process_crash_test.py",
    ],
)

py_test(
    name = "agent_sessions_api_test",
    srcs = ["//projects/monolith/agent_sessions:api_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_progress_ingest_test",
    srcs = ["//projects/monolith/agent_sessions:progress_ingest_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_result_receipts_test",
    srcs = ["//projects/monolith/agent_sessions:result_receipts_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_preinvoke_test",
    srcs = ["//projects/monolith/agent_sessions:preinvoke_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        ":pkg_core",
        ":pkg_cyclegroup",
        ":pkg_knowledge",
        ":pkg_swarm",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_receipt_capture_test",
    srcs = ["//projects/monolith/agent_sessions:receipt_capture_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        ":pkg_core",
        ":pkg_swarm",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_receipt_consumer_test",
    srcs = ["//projects/monolith/agent_sessions:receipt_consumer_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        ":pkg_core",
        ":pkg_swarm",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_response_lost_test",
    srcs = ["//projects/monolith/agent_sessions:response_lost_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        ":pkg_core",
        ":pkg_swarm",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_router_backfill_exception_test",
    srcs = ["//projects/monolith/chat:router_backfill_exception_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_backfill_flush_test",
    srcs = ["//projects/monolith/chat:backfill_flush_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_find_user_test",
    srcs = ["//projects/monolith/chat:store_find_user_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_search_model_validate_test",
    srcs = ["//projects/monolith/chat:store_search_model_validate_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_thinking_view_callback_test",
    srcs = ["//projects/monolith/chat:bot_thinking_view_callback_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_store_lock_test",
    srcs = ["//projects/monolith/chat:store_lock_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_bot_reprocess_test",
    srcs = ["//projects/monolith/chat:bot_reprocess_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "main_lock_sweep_test",
    srcs = ["//projects/monolith/app:main_lock_sweep_test.py"],
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
    srcs = ["//projects/monolith/app:main_app_state_test.py"],
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
    name = "scheduler_test",
    srcs = ["//projects/monolith/scheduler:scheduler_test.py"],
    imports = ["."],
    deps = [
        ":pkg_scheduler",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "scheduler_argo_handled_test",
    srcs = ["//projects/monolith/scheduler:argo_handled_test.py"],
    imports = ["."],
    deps = [
        ":pkg_scheduler",
        "@pip//pytest",
    ],
)

py_test(
    name = "main_log_coverage_test",
    srcs = ["//projects/monolith/app:main_log_coverage_test.py"],
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
    srcs = ["//projects/monolith/home:schedule_calendar_handler_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//icalendar",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//tzdata",
    ],
)

py_test(
    name = "shared_forecast_freshness_test",
    srcs = ["//projects/monolith/shared:forecast_freshness_test.py"],
    imports = ["."],
    deps = [
        ":pkg_shared",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_chunker_test",
    srcs = ["//projects/monolith/knowledge:chunker_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_chunker_extra_test",
    srcs = ["//projects/monolith/knowledge:chunker_extra_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_chunker_split_edge_cases_test",
    srcs = ["//projects/monolith/knowledge:chunker_split_edge_cases_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_frontmatter_test",
    srcs = ["//projects/monolith/knowledge:frontmatter_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

py_test(
    name = "knowledge_frontmatter_scoped_test",
    srcs = ["//projects/monolith/knowledge:frontmatter_scoped_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_layout_test",
    srcs = ["//projects/monolith/knowledge:layout_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//networkx",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_links_test",
    srcs = ["//projects/monolith/knowledge:links_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_links_extra_test",
    srcs = ["//projects/monolith/knowledge:links_extra_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_links_coverage_test",
    srcs = ["//projects/monolith/knowledge:links_coverage_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_store_test",
    srcs = ["//projects/monolith/knowledge:store_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pgvector",
        "@pip//pydantic",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_store_scoped_test",
    srcs = ["//projects/monolith/knowledge:store_scoped_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_indexing_test",
    srcs = ["//projects/monolith/knowledge:indexing_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "knowledge_wikilinks_test",
    srcs = ["//projects/monolith/knowledge:wikilinks_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_visibility_test",
    srcs = ["//projects/monolith/knowledge:visibility_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_profile_test",
    srcs = ["//projects/monolith/knowledge:profile_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_tools_add_visibility_field_test",
    srcs = ["//projects/monolith/knowledge:tools/add_visibility_field_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_tools_classify_visibility_backfill_test",
    srcs = ["//projects/monolith/knowledge:tools/classify_visibility_backfill_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_gen_repo_docs_manifest_test",
    srcs = ["//projects/monolith/knowledge:gen_repo_docs_manifest_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_gen_docs_manifest_test",
    srcs = ["//projects/monolith/knowledge:gen_docs_manifest_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_gen_posts_manifest_test",
    srcs = ["//projects/monolith/knowledge:gen_posts_manifest_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_tools_prettier_json_test",
    srcs = ["//projects/monolith/knowledge:tools/prettier_json_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_gardener_test",
    srcs = ["//projects/monolith/knowledge:gardener_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_models_test",
    srcs = ["//projects/monolith/knowledge:models_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "grimoire_models_test",
    srcs = ["//projects/monolith/grimoire:models_test.py"],
    imports = ["."],
    deps = [
        ":pkg_grimoire",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "grimoire_visibility_test",
    srcs = ["//projects/monolith/grimoire:visibility_test.py"],
    imports = ["."],
    deps = [
        ":pkg_grimoire",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "grimoire_router_test",
    srcs = ["//projects/monolith/grimoire:router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_grimoire",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "grimoire_entities_test",
    srcs = ["//projects/monolith/grimoire:entities_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_grimoire",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "grimoire_search_test",
    srcs = ["//projects/monolith/grimoire:search_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_grimoire",
        ":pkg_knowledge",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "grimoire_library_test",
    srcs = ["//projects/monolith/grimoire:library_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_grimoire",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "grimoire_public_test",
    srcs = ["//projects/monolith/grimoire:public_test.py"],
    imports = ["."],
    deps = [
        ":pkg_grimoire",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "grimoire_explore_test",
    srcs = ["//projects/monolith/grimoire:explore_test.py"],
    imports = ["."],
    deps = [
        ":pkg_grimoire",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "grimoire_router_public_test",
    srcs = ["//projects/monolith/grimoire:router_public_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_grimoire",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "grimoire_ingest_test",
    srcs = ["//projects/monolith/grimoire:ingest_test.py"],
    imports = ["."],
    deps = [
        ":pkg_grimoire",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "grimoire_extract_test",
    srcs = ["//projects/monolith/grimoire:extract_test.py"],
    imports = ["."],
    deps = [
        ":pkg_grimoire",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "grimoire_marker_test",
    srcs = ["//projects/monolith/grimoire:marker_test.py"],
    imports = ["."],
    deps = [
        ":pkg_grimoire",
        "@pip//pytest",
    ],
)

py_test(
    name = "grimoire_jobs_test",
    srcs = ["//projects/monolith/grimoire:jobs_test.py"],
    imports = ["."],
    deps = [
        ":pkg_grimoire",
        "@pip//httpx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "grimoire_hierarchy_backfill_test",
    srcs = ["//projects/monolith/grimoire:hierarchy_backfill_test.py"],
    imports = ["."],
    deps = [
        ":pkg_grimoire",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "ships_models_test",
    srcs = ["//projects/monolith/ships:models_test.py"],
    imports = ["."],
    deps = [
        ":pkg_ships",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "moving_models_test",
    srcs = [
        "//projects/monolith/moving:tests/__init__.py",
        "//projects/monolith/moving:tests/conftest.py",
        "//projects/monolith/moving:tests/models_test.py",
    ],
    data = ["//projects/monolith/chart:migrations"],
    imports = ["."],
    deps = [
        ":pkg_moving",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "moving_collisions_test",
    srcs = [
        "//projects/monolith/moving:tests/__init__.py",
        "//projects/monolith/moving:tests/collisions_test.py",
    ],
    imports = ["."],
    deps = [
        ":pkg_moving",
        "@pip//pytest",
    ],
)

py_test(
    name = "moving_viewer_test",
    srcs = [
        "//projects/monolith/moving:tests/__init__.py",
        "//projects/monolith/moving:tests/conftest.py",
        "//projects/monolith/moving:tests/viewer_test.py",
    ],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_moving",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "moving_router_test",
    srcs = [
        "//projects/monolith/moving:tests/__init__.py",
        "//projects/monolith/moving:tests/conftest.py",
        "//projects/monolith/moving:tests/router_test.py",
    ],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_moving",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "moving_chat_test",
    srcs = [
        "//projects/monolith/moving:tests/__init__.py",
        "//projects/monolith/moving:tests/chat_test.py",
        "//projects/monolith/moving:tests/conftest.py",
    ],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_moving",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "moving_gcal_sync_test",
    srcs = [
        "//projects/monolith/moving:tests/__init__.py",
        "//projects/monolith/moving:tests/conftest.py",
        "//projects/monolith/moving:tests/gcal_sync_test.py",
    ],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_moving",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "updates_test",
    srcs = ["//projects/monolith/updates:updates_test.py"],
    imports = ["."],
    deps = [
        ":pkg_auth",
        ":pkg_core",
        ":pkg_updates",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pydantic",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "hikes_models_test",
    srcs = ["//projects/monolith/hikes:models_test.py"],
    imports = ["."],
    deps = [
        ":pkg_hikes",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "faas_models_test",
    srcs = ["//projects/monolith/faas:models_test.py"],
    imports = ["."],
    deps = [
        ":pkg_faas",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "faas_storage_test",
    srcs = ["//projects/monolith/faas:storage_test.py"],
    imports = ["."],
    deps = [
        ":pkg_faas",
        "@pip//boto3",
        "@pip//pytest",
    ],
)

py_test(
    name = "faas_runtime_test",
    srcs = ["//projects/monolith/faas:runtime_test.py"],
    imports = ["."],
    deps = [
        ":pkg_faas",
        "@pip//pytest",
    ],
)

py_test(
    name = "faas_embervm_client_test",
    srcs = ["//projects/monolith/faas:embervm_client_test.py"],
    imports = ["."],
    deps = [
        ":pkg_faas",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "faas_workload_test",
    srcs = ["//projects/monolith/faas:workload_test.py"],
    imports = ["."],
    deps = [
        ":pkg_faas",
        "@pip//kubernetes_asyncio",
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

py_test(
    name = "faas_reconcile_test",
    srcs = ["//projects/monolith/faas:reconcile_test.py"],
    imports = ["."],
    deps = [
        ":pkg_faas",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "faas_router_test",
    srcs = ["//projects/monolith/faas:router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_faas",
        ":pkg_framework",
        "@pip//fastapi",
        "@pip//pytest",
        "@pip//python_multipart",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "faas_invoke_router_test",
    srcs = ["//projects/monolith/faas:invoke_router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_faas",
        ":pkg_framework",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "faas_invoke_router_public_test",
    srcs = ["//projects/monolith/faas:invoke_router_public_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_faas",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "stars_models_test",
    srcs = ["//projects/monolith/stars:models_test.py"],
    imports = ["."],
    deps = [
        ":pkg_stars",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "stars_scoring_test",
    srcs = ["//projects/monolith/stars:scoring_test.py"],
    imports = ["."],
    deps = [
        ":pkg_stars",
        "@pip//pydantic",
        "@pip//pytest",
    ],
)

py_test(
    name = "stars_forecast_test",
    srcs = ["//projects/monolith/stars:forecast_test.py"],
    imports = ["."],
    deps = [
        ":pkg_stars",
        "@pip//pytest",
    ],
)

py_test(
    name = "stars_jobs_test",
    srcs = ["//projects/monolith/stars:jobs_test.py"],
    imports = ["."],
    deps = [
        ":pkg_shared",
        ":pkg_stars",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "stars_grid_test",
    srcs = ["//projects/monolith/stars:grid_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_stars",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "stars_climatology_test",
    srcs = ["//projects/monolith/stars:climatology_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_stars",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "stars_router_test",
    srcs = ["//projects/monolith/stars:router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_shared",
        ":pkg_stars",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "stars_health_test",
    srcs = ["//projects/monolith/stars:health_test.py"],
    imports = ["."],
    deps = [
        ":pkg_stars",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "hikes_walkhighlands_test",
    srcs = ["//projects/monolith/hikes:walkhighlands_test.py"],
    imports = ["."],
    deps = [
        ":pkg_hikes",
        "@pip//pytest",
    ],
)

py_test(
    name = "hikes_forecast_test",
    srcs = ["//projects/monolith/hikes:forecast_test.py"],
    imports = ["."],
    deps = [
        ":pkg_hikes",
        "@pip//pytest",
    ],
)

py_test(
    name = "hikes_router_test",
    srcs = ["//projects/monolith/hikes:router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_hikes",
        ":pkg_shared",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

# Hand-registered (chat_public is gazelle-excluded): fast SQLite + TestClient
# unit tests for the public-chat session lifecycle, budgets, and SSE shape.
py_test(
    name = "chat_public_router_test",
    srcs = ["//projects/monolith/chat_public:router_test.py"],
    imports = ["."],
    deps = [
        "//projects/monolith/public_backend:monolith_public_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_public_health_test",
    srcs = ["//projects/monolith/chat_public:health_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

# Artifact service: TestClient unit tests for the publish and read endpoints
# (S3 faked) and the S3 helper (boto3 mocked).
py_test(
    name = "artifact_router_test",
    srcs = ["//projects/monolith/artifact:router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_artifact",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "artifact_s3_test",
    srcs = ["//projects/monolith/artifact:s3_test.py"],
    imports = ["."],
    deps = [
        ":pkg_artifact",
        "@pip//boto3",
        "@pip//pytest",
    ],
)

# Hand-registered (chat_public is gazelle-excluded): opt-in read-only transcript
# snapshots ("share this chat"): server-side mint from the stored transcript,
# round-trip via the read route, empty-transcript -> 400, missing -> 404.
py_test(
    name = "chat_public_snapshots_test",
    srcs = ["//projects/monolith/chat_public:snapshots_test.py"],
    imports = ["."],
    deps = [
        "//projects/monolith/public_backend:monolith_public_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

# Hand-registered (chat_public is gazelle-excluded): Phase 2 admission + abuse
# controls (Turnstile siteverify failure -> 403, global circuit-breaker shed via
# the busy SSE event, salted ip_hash storage).
py_test(
    name = "chat_public_phase2_test",
    srcs = ["//projects/monolith/chat_public:phase2_test.py"],
    imports = ["."],
    deps = [
        "//projects/monolith/public_backend:monolith_public_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

# Hand-registered (chat_public is gazelle-excluded): Phase 3 real inference +
# compaction (streaming SSE shape, server-fixed system prompt, real-usage token
# ceiling, rolling-summary compaction, in-flight slot release after stream).
py_test(
    name = "chat_public_phase3_test",
    srcs = ["//projects/monolith/chat_public:phase3_test.py"],
    imports = ["."],
    deps = [
        "//projects/monolith/public_backend:monolith_public_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

# Hand-registered (chat_public is gazelle-excluded): Phase 4a public-graph
# retrieval (mocked embedding + chunk search): delimited retrieved-context block,
# no-tools inference payload, node_touched-per-note SSE before tokens, empty
# retrieval still streams, and retrieve() embed+map+fail-open behaviour.
py_test(
    name = "chat_public_phase4_test",
    srcs = ["//projects/monolith/chat_public:phase4_test.py"],
    imports = ["."],
    deps = [
        "//projects/monolith/public_backend:monolith_public_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

# Hand-registered (chat_public is gazelle-excluded): Phase 5 in-process response
# cache (repeated identical message hits the cache and skips vLLM; a changed
# notes watermark or prompt/model version misses; a cache hit still emits
# token+done and persists the turn).
py_test(
    name = "chat_public_phase5_test",
    srcs = ["//projects/monolith/chat_public:phase5_test.py"],
    imports = ["."],
    deps = [
        "//projects/monolith/public_backend:monolith_public_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

# Hand-registered (grimoire_chat is gazelle-excluded): Grimoire-corpus retrieval
# (mocked embedding + stubbed knn_embeddings): embedding-model-space match (the
# stored corpus model is passed to knn), is_global filtering drops campaign-private
# entity hits, chunk/entity hits map to RetrievedPassages, blank query + embedder
# error fail open to an ungrounded turn.
py_test(
    name = "grimoire_chat_retrieval_test",
    srcs = ["//projects/monolith/grimoire_chat:retrieval_test.py"],
    imports = ["."],
    deps = [
        "//projects/monolith/public_backend:monolith_public_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

# Hand-registered (grimoire_chat is gazelle-excluded): router smoke tests over the
# SQLite create_all fixture: the retrieved corpus is fenced as a <sourcebooks> DATA
# block labelled "not instructions", the inference payload carries no tools, one
# node_touched per retrieved passage precedes the tokens, and the D&D system prompt
# is server-fixed + env-overridable.
py_test(
    name = "grimoire_chat_router_test",
    srcs = ["//projects/monolith/grimoire_chat:router_test.py"],
    imports = ["."],
    deps = [
        "//projects/monolith/public_backend:monolith_public_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

# Guard (ADR 005): the Turnstile secret stays in the backend; the frontend
# carries only the public site-key literal. Reads the monolith-public chart's
# values.yaml via a cross-package data dep (no Helm toolchain needed).
py_test(
    name = "public_turnstile_secret_isolation_test",
    srcs = ["public_turnstile_secret_isolation_test.py"],
    data = ["//projects/monolith-public/chart:chart_values"],
    imports = ["."],
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

# Guard (ADR 005): the public HTTPRoute (in the monolith-public chart) must never
# expose chat or the internal API. Lives here, not in the chart package, because
# that package has no Python toolchain context; it reads the template via a
# cross-package data dep.
py_test(
    name = "public_httproute_chat_guard_test",
    srcs = ["public_httproute_chat_guard_test.py"],
    data = ["//projects/monolith-public/chart:httproute_public_template"],
    imports = ["."],
    deps = ["@pip//pytest"],
)

# Guard (#5142): the public tier's destination-scoped Cilium egress policies
# must never regain `toEntities: cluster`, and the ENFORCED render must allow
# exactly the audited dependency set. Same cross-package data-dep shape as the
# HTTPRoute guard, plus what `helm template` needs: Chart.lock and the charts/
# glob (helm aborts when a locked subchart tarball is missing; this chart has
# no migrations/ directory), the deploy values, and the helm binary.
py_test(
    name = "public_cilium_scoped_egress_guard_test",
    srcs = ["public_cilium_scoped_egress_guard_test.py"],
    data = [
        "//projects/monolith-public/chart:chart_render_inputs",
        "//projects/monolith-public/chart:cilium_scoped_egress_template",
        "//projects/monolith-public/deploy:deploy_values",
        "@multitool//tools/helm",
    ],
    env = {
        "HELM_BIN": "$(rootpath @multitool//tools/helm)",
        "DEPLOY_VALUES": "$(rootpath //projects/monolith-public/deploy:deploy_values)",
    },
    imports = ["."],
    deps = ["@pip//pyyaml"],
)

# Guard: the packaged homelab-library must deploy images by content digest, not
# by the build-timestamped tag that push-changed.sh frequently never pushes.
# Reads the packaged tarball (what actually ships) rather than the library
# source, so a fix that was never re-synced into the tarball still fails.
py_test(
    name = "public_library_image_digest_guard_test",
    srcs = ["public_library_image_digest_guard_test.py"],
    data = ["//projects/monolith-public/chart:packaged_library_chart"],
    imports = ["."],
)

py_test(
    name = "hikes_jobs_test",
    srcs = ["//projects/monolith/hikes:jobs_test.py"],
    imports = ["."],
    deps = [
        ":pkg_hikes",
        ":pkg_shared",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "hikes_init_test",
    srcs = ["//projects/monolith/hikes:__init___test.py"],
    imports = ["."],
    deps = [
        ":pkg_hikes",
        "@pip//pytest",
    ],
)

py_test(
    name = "dr_jobs_models_test",
    srcs = ["//projects/monolith/dr_jobs:models_test.py"],
    imports = ["."],
    deps = [
        ":pkg_dr_jobs",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "dr_jobs_scraper_test",
    srcs = ["//projects/monolith/dr_jobs:scraper_test.py"],
    imports = ["."],
    deps = [
        ":pkg_dr_jobs",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "dr_jobs_router_test",
    srcs = ["//projects/monolith/dr_jobs:router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_dr_jobs",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "dr_jobs_jobs_test",
    srcs = ["//projects/monolith/dr_jobs:jobs_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_dr_jobs",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "worldcup_ratings_test",
    srcs = ["//projects/monolith/worldcup:ratings_test.py"],
    # ratings.load_elo() reads the committed Elo snapshot at runtime.
    data = ["//projects/monolith/worldcup:ratings/elo_2026.json"],
    imports = ["."],
    deps = [
        ":pkg_worldcup",
        "@pip//pytest",
    ],
)

py_test(
    name = "worldcup_outcome_test",
    srcs = ["//projects/monolith/worldcup:outcome_test.py"],
    imports = ["."],
    deps = [
        ":pkg_worldcup",
        "@pip//pytest",
    ],
)

py_test(
    name = "worldcup_sim_test",
    srcs = ["//projects/monolith/worldcup:sim_test.py"],
    data = ["//projects/monolith/worldcup:ratings/elo_2026.json"],
    imports = ["."],
    deps = [
        ":pkg_worldcup",
        "@pip//pytest",
    ],
)

py_test(
    name = "worldcup_strength_test",
    srcs = ["//projects/monolith/worldcup:strength_test.py"],
    imports = ["."],
    deps = [
        ":pkg_worldcup",
        "@pip//pytest",
    ],
)

py_test(
    name = "worldcup_client_test",
    srcs = ["//projects/monolith/worldcup:client_test.py"],
    imports = ["."],
    deps = [
        ":pkg_worldcup",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "worldcup_jobs_test",
    srcs = ["//projects/monolith/worldcup:jobs_test.py"],
    data = ["//projects/monolith/worldcup:ratings/elo_2026.json"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_worldcup",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "worldcup_router_test",
    srcs = ["//projects/monolith/worldcup:router_test.py"],
    data = ["//projects/monolith/worldcup:ratings/elo_2026.json"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_worldcup",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "campsites_client_test",
    srcs = ["//projects/monolith/campsites:client_test.py"],
    # The load_catalog / _read_catalog_json tests read the committed static
    # catalog from runfiles, so ship it as test data.
    data = ["//projects/monolith/campsites:catalog.json"],
    imports = ["."],
    deps = [
        ":pkg_campsites",
        "@pip//pytest",
    ],
)

py_test(
    name = "campsites_weather_test",
    srcs = ["//projects/monolith/campsites:weather_test.py"],
    imports = ["."],
    deps = [
        ":pkg_campsites",
        "@pip//pytest",
    ],
)

py_test(
    name = "campsites_jobs_test",
    srcs = ["//projects/monolith/campsites:jobs_test.py"],
    imports = ["."],
    deps = [
        ":pkg_campsites",
        ":pkg_core",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "campsites_router_test",
    srcs = ["//projects/monolith/campsites:router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_campsites",
        ":pkg_core",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "stars_init_test",
    srcs = ["//projects/monolith/stars:__init___test.py"],
    imports = ["."],
    deps = [
        ":pkg_stars",
        "@pip//pytest",
    ],
)

py_test(
    name = "hikes_generate_seed_test",
    srcs = [
        "//projects/monolith/hikes:tools/generate_seed.py",
        "//projects/monolith/hikes:tools/generate_seed_test.py",
    ],
    imports = ["hikes/tools"],
    deps = ["@pip//pytest"],
)

py_test(
    name = "stars_generate_grid_test",
    srcs = [
        "//projects/monolith/stars:grid_gen/generate_grid.py",
        "//projects/monolith/stars:grid_gen/generate_grid_test.py",
    ],
    imports = ["stars/grid_gen"],
    deps = ["@pip//pytest"],
)

py_test(
    name = "stars_generate_grid_v2_test",
    srcs = [
        "//projects/monolith/stars:grid_gen/generate_grid_v2.py",
        "//projects/monolith/stars:grid_gen/generate_grid_v2_test.py",
    ],
    imports = ["stars/grid_gen"],
    deps = ["@pip//pytest"],
)

py_test(
    name = "ships_heat_test",
    srcs = ["//projects/monolith/ships:heat_test.py"],
    imports = ["."],
    deps = [
        ":pkg_ships",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "ships_ais_test",
    srcs = ["//projects/monolith/ships:ais_test.py"],
    imports = ["."],
    deps = [
        ":pkg_ships",
        "@pip//pytest",
    ],
)

py_test(
    name = "ships_store_test",
    srcs = ["//projects/monolith/ships:store_test.py"],
    imports = ["."],
    deps = [
        ":pkg_ships",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "ships_ingest_test",
    srcs = ["//projects/monolith/ships:ingest_test.py"],
    imports = ["."],
    deps = [
        ":pkg_ships",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//websockets",
    ],
)

py_test(
    name = "ships_router_test",
    srcs = ["//projects/monolith/ships:router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_ships",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "ships_retention_test",
    srcs = ["//projects/monolith/ships:retention_test.py"],
    imports = ["."],
    deps = [
        ":pkg_ships",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_gap_model_test",
    srcs = ["//projects/monolith/knowledge:gap_model_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_gap_set_class_test",
    srcs = ["//projects/monolith/knowledge:gap_set_class_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_gap_answer_fileless_test",
    srcs = ["//projects/monolith/knowledge:gap_answer_fileless_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_store_gap_queries_test",
    srcs = ["//projects/monolith/knowledge:store_gap_queries_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_service_test",
    srcs = ["//projects/monolith/knowledge:service_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        ":pkg_scheduler",
        "@pip//networkx",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

sh_binary(
    name = "generate-routes",
    srcs = ["generate-routes.sh"],
    visibility = ["//bazel/tools/format:__pkg__"],
)

py_test(
    name = "chat_backfill_extra_test",
    srcs = ["//projects/monolith/chat:backfill_extra_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "knowledge_store_extra_test",
    srcs = ["//projects/monolith/knowledge:store_extra_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_frontmatter_extra_test",
    srcs = ["//projects/monolith/knowledge:frontmatter_extra_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

py_test(
    name = "knowledge_frontmatter_json_safe_test",
    srcs = ["//projects/monolith/knowledge:frontmatter_json_safe_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

py_test(
    name = "chat_store_reclaim_race_test",
    srcs = ["//projects/monolith/chat:store_reclaim_race_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "home_schedule_naive_dtend_test",
    srcs = ["//projects/monolith/home:schedule_naive_dtend_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//icalendar",
        "@pip//pytest",
        "@pip//tzdata",
    ],
)

py_test(
    name = "knowledge_store_zero_chunk_test",
    srcs = ["//projects/monolith/knowledge:store_zero_chunk_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_store_optional_metadata_test",
    srcs = ["//projects/monolith/knowledge:store_optional_metadata_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_raw_paths_test",
    srcs = ["//projects/monolith/knowledge:raw_paths_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_migrate_raw_bucketing_test",
    srcs = ["//projects/monolith/knowledge:migrate_raw_bucketing_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_fixes_coverage_test",
    srcs = ["//projects/monolith/knowledge:knowledge_fixes_coverage_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//pyyaml",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_router_test",
    srcs = ["//projects/monolith/knowledge:router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_framework",
        ":pkg_knowledge",
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
    srcs = ["//projects/monolith/knowledge:router_raws_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_framework",
        ":pkg_knowledge",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_redact_test",
    srcs = ["//projects/monolith/knowledge:redact_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_publish_test",
    srcs = ["//projects/monolith/knowledge:publish_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_notes_crud_test",
    srcs = ["//projects/monolith/knowledge:notes_crud_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_framework",
        ":pkg_knowledge",
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
    srcs = ["//projects/monolith/knowledge:mcp_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//fastmcp",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "knowledge_mcp_registry_test",
    srcs = ["//projects/monolith/knowledge:mcp_registry_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//fastmcp",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "knowledge_mcp_reports_test",
    srcs = ["//projects/monolith/knowledge:mcp_reports_test.py"],
    imports = ["."],
    deps = [
        ":pkg_auth",
        ":pkg_cyclegroup",
        ":pkg_knowledge",
        "@pip//fastmcp",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_interventions_test",
    srcs = ["//projects/monolith/knowledge:interventions_test.py"],
    imports = ["."],
    deps = [
        ":pkg_auth",
        ":pkg_core",
        ":pkg_cyclegroup",
        ":pkg_knowledge",
        ":swarm_factory_controls",
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
    srcs = ["//projects/monolith/knowledge:gap_api_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_knowledge",
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
    srcs = ["//projects/monolith/knowledge:gap_review_endpoints_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_knowledge",
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
    srcs = ["//projects/monolith/knowledge:note_review_endpoints_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_knowledge",
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
    srcs = ["//projects/monolith/knowledge:notes_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pyyaml",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "tasks_router_test",
    srcs = ["//projects/monolith/knowledge:tasks_router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_framework",
        ":pkg_knowledge",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_raw_store_test",
    srcs = ["//projects/monolith/knowledge:raw_store_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//boto3",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_store_note_links_test",
    srcs = ["//projects/monolith/knowledge:store_note_links_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_ingest_queue_claim_test",
    srcs = ["//projects/monolith/knowledge:ingest_queue_claim_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_ingest_queue_webpage_title_test",
    srcs = ["//projects/monolith/knowledge:ingest_queue_webpage_title_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_web_search_type_error_test",
    srcs = ["//projects/monolith/chat:web_search_type_error_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "sse_test",
    srcs = ["//projects/monolith/chat:sse_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//anyio",
        "@pip//pytest",
    ],
)

py_test(
    name = "explorer_test",
    srcs = ["//projects/monolith/chat:explorer_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//anyio",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
    ],
)

py_test(
    name = "explore_router_test",
    srcs = ["//projects/monolith/chat:explore_router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        ":pkg_framework",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
    ],
)

py_test(
    name = "cluster_agent_test",
    srcs = ["//projects/monolith/chat:cluster_agent_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cluster",
        ":pkg_cyclegroup",
        "@pip//anyio",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_ingest_queue_router_test",
    srcs = ["//projects/monolith/knowledge:ingest_queue_router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_framework",
        ":pkg_knowledge",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_dead_letter_test",
    srcs = ["//projects/monolith/knowledge:dead_letter_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_framework",
        ":pkg_knowledge",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "tasks_store_test",
    srcs = ["//projects/monolith/knowledge:tasks_store_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "tasks_rollup_test",
    srcs = ["//projects/monolith/knowledge:tasks_rollup_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "home_observability_slo_test",
    srcs = ["//projects/monolith/home:observability/slo_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "home_observability_router_test",
    srcs = ["//projects/monolith/home:observability/router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_cyclegroup",
        ":pkg_framework",
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
        # stars/grid.py's _s3_client guards endpoint_url with a scheme check
        # (prepends http:// when absent), mirroring the lakehouse S3 client, so
        # the runtime "Invalid endpoint" risk is handled. semgrep-core does not
        # honour the inline # nosemgrep, so exclude the rule here.
        "boto3-endpoint-url-missing-scheme",  # keep
        # The monolith uses datetime | None = None for intentional NULL state
        # fields (e.g. last_run_at, locked_at, updated_at). semgrep-core
        # invokes the rule engine directly without pysemgrep, so # nosemgrep
        # inline comments are not honoured. Exclude until per-finding
        # suppression is available in the core engine.
        "sqlmodel-datetime-without-factory",  # keep
        # test-hardcoded-past-timestamp's own paths.include is
        # [*_test.py, test_*.py] -- the rule is intended for test files
        # only. The semgrep_target_test macro runs rules without
        # honouring per-rule path filters, so the rule fires on
        # production files like knowledge/profile.py that have
        # intentional audit-trail date constants (PROFILE_UPDATED).
        # Excluded from main_semgrep_test (which scans :main, production
        # code); the rule still applies wherever it's meant to.
        "test-hardcoded-past-timestamp",  # keep
        "tainted-fastapi-http-request-httpx",
        # note_id → DB lookup → note["path"] → file read is guarded by
        # is_relative_to(vault_root) but Semgrep Pro can't trace through
        # the DB as a sanitizer.
        "tainted-path-traversal-stdlib-fastapi",  # keep
        # trips/ingest.py validate_image does Image.open(io.BytesIO(data)) on
        # in-memory upload bytes (NOT a filesystem path), so there is no path
        # traversal; the rule false-positives on any Image.open of request data.
        "tainted-path-traversal-pillow-fastapi",  # keep
        # shotter/hosts.py's HOST_SERVICE_MAP is the baked mapping design ADR
        # embervm/035 section 3 and 4 call for on purpose: a Cloudflare-routed
        # lookup would faithfully screenshot private.jomcgi.dev's login wall
        # instead of the page behind it, so the map has to name the exact
        # in-cluster Service DNS the guest's own proxy tunnels to, not a
        # release-name-relative default read from an env var. The two entries
        # are also asserted exact (size and values) by the domain's own tests,
        # so a release rename would fail loudly here rather than silently
        # resolving wrong. semgrep-core does not honour inline # nosemgrep, so
        # excluded here.
        "no-hardcoded-k8s-service-url",  # keep
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
    target = ":main",
)

py_test(
    name = "home_observability_stats_test",
    srcs = ["//projects/monolith/home:observability/stats_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "home_observability_traces_test",
    srcs = ["//projects/monolith/home:observability/traces_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "demos_firecracker_api_test",
    srcs = ["//projects/monolith/demos:firecracker_api_test.py"],
    imports = ["."],
    deps = [
        ":pkg_demos",
        ":pkg_ember_public",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

# Hand-registered (ember_public is gazelle-excluded): mounts the demo-postgres
# router standalone plus asserts it against the composed public app (proving
# both the route and the demos-absence guarantee).
py_test(
    name = "ember_public_router_test",
    srcs = ["//projects/monolith/ember_public:router_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

# Hand-registered (ember_public is gazelle-excluded): the bazel skyframe
# query demo core (validation, session rate limit, semaphore, error mapping).
py_test(
    name = "ember_public_bazel_core_test",
    srcs = ["//projects/monolith/ember_public:bazel_core_test.py"],
    imports = ["."],
    deps = [
        ":pkg_ember_public",
        ":pkg_faas",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

# Hand-registered (ember_public is gazelle-excluded): the /api/health component
# registration and the synthetic probe's fail-closed unconfigured branch. This
# file had no target until 2026-07-28, so nothing in it ran; gazelle cannot
# catch that here because the whole directory is excluded from it.
py_test(
    name = "ember_public_health_test",
    srcs = ["//projects/monolith/ember_public:health_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "ember_public_synthetic_health_test",
    srcs = ["//projects/monolith/ember_public:synthetic_health_test.py"],
    imports = ["."],
    deps = [
        ":pkg_ember_public",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "ember_public_synthetic_probe_test",
    srcs = ["//projects/monolith/ember_public:synthetic_probe_test.py"],
    imports = ["."],
    deps = [
        # probe_codex imports agent_sessions.api lazily, inside the function, so
        # the public binary never pulls it in at module load. The private probe
        # endpoint has the package at runtime, and this test needs the module on
        # its path so monkeypatch can resolve run_synthetic_session.
        ":pkg_agent_sessions",
        ":pkg_ember_public",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "ember_public_synthetic_router_test",
    srcs = ["//projects/monolith/ember_public:synthetic_router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_ember_public",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "demos_loadtest_corpus_test",
    srcs = ["//projects/monolith/demos:loadtest_corpus_test.py"],
    data = ["//projects/monolith/demos:loadtest_samples"],
    imports = ["."],
    deps = [
        ":pkg_demos",
        "@pip//pytest",
    ],
)

py_test(
    name = "demos_loadtest_test",
    srcs = ["//projects/monolith/demos:loadtest_test.py"],
    # run_load_test defaults its corpus to load_corpus(workload), which reads
    # the committed .sample files at runtime.
    data = ["//projects/monolith/demos:loadtest_samples"],
    imports = ["."],
    deps = [
        ":pkg_demos",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "home_observability_rollup_test",
    srcs = ["//projects/monolith/home:observability/rollup_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "cluster_kubernetes_test",
    srcs = ["//projects/monolith/cluster:kubernetes_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cluster",
        "@pip//kubernetes_asyncio",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "core_platform_probe_test",
    srcs = ["//projects/monolith/core:platform_probe_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "cluster_cd_health_test",
    srcs = ["//projects/monolith/cluster:cd_health_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cluster",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "cluster_recovery_startup_test",
    srcs = ["//projects/monolith/cluster:recovery_startup_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cluster",
        ":pkg_cyclegroup",
        ":pkg_framework",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "scheduler_views_test",
    srcs = ["//projects/monolith/scheduler:views_test.py"],
    imports = ["."],
    deps = [
        ":pkg_scheduler",
        "@pip//pydantic",
        "@pip//pytest",
    ],
)

py_test(
    name = "scheduler_service_test",
    srcs = ["//projects/monolith/scheduler:service_test.py"],
    imports = ["."],
    deps = [
        ":pkg_scheduler",
        "@pip//fastapi",
        "@pip//kubernetes_asyncio",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "scheduler_router_test",
    srcs = ["//projects/monolith/scheduler:router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_framework",
        ":pkg_scheduler",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

bdd_test(
    name = "home_bdd_api_test",
    srcs = [
        "//projects/monolith/home:tests/__init__.py",
        "//projects/monolith/home:tests/bdd_api_test.py",
        "//projects/monolith/home:tests/conftest.py",
    ],
)

# Real-Postgres test: asserts the public_reader role's GRANTs isolate private rows
# (ADR 004). Uses the bdd_test harness for the pg fixture + applied migrations.
bdd_test(
    name = "public_reader_grants_test",
    srcs = ["public_reader_grants_test.py"],
)

# Real-Postgres test: the observability rollup writer upserts a snapshot and
# public_reader can read it (ADR 004 Layer 4).
bdd_test(
    name = "observability_snapshot_grants_test",
    srcs = ["observability_snapshot_grants_test.py"],
)

# Real-Postgres test: the public_api knowledge views derive public-only rows,
# public_reader can read them but not the knowledge schema, and the public
# endpoints filter private targets / strip private wikilinks (ADR 004 5a').
bdd_test(
    name = "public_knowledge_views_test",
    srcs = ["public_knowledge_views_test.py"],
)

# Real-Postgres test: agent activity views expose aggregate columns only,
# tolerate empty usage payloads, and grant public_reader only the views.
bdd_test(
    name = "public_agent_activity_views_test",
    srcs = ["//projects/monolith/agent_sessions:public_agent_activity_views_test.py"],
    deps = ["@pip//typer"],
)

# Real-Postgres test: the public factory snapshot WRITER. Covers the empty
# board (both prune keep-lists empty, which is the untyped-bindparam case),
# the seeded board's task and session rows, identity never reaching a payload,
# and a task falling off the window taking its sessions with it. The shaping
# half is agent_sessions_factory_public_test, which needs no database.
bdd_test(
    name = "factory_public_writer_test",
    srcs = ["//projects/monolith/agent_sessions:factory_public_writer_test.py"],
)

# Real-Postgres test: the public_writer role can DML only the chat_public schema,
# is denied other schemas, and public_reader can never write chat_public
# (ADR 005). Uses the bdd_test harness for the pg fixture + applied migrations.
bdd_test(
    name = "chat_public_grants_test",
    srcs = ["chat_public_grants_test.py"],
)

# Real-Postgres test: the public_api.knowledge_chunks view exposes only chunks of
# public, non-deleted notes, public_reader can read the view but not the underlying
# knowledge.chunks table, so public-chat retrieval is DB-confined (ADR 005 layer 5).
bdd_test(
    name = "public_api_chunks_grants_test",
    srcs = ["public_api_chunks_grants_test.py"],
)

# Real-Postgres test: public_writer can SELECT/INSERT/UPDATE demo_pg_savings
# (the accrual write path used by both tiers' status poll) and public_reader
# can SELECT it (the cached GET /savings endpoint) but not write it (Task 3,
# ADR security/005's narrow-grant precedent).
bdd_test(
    name = "ember_public_savings_grants_test",
    srcs = ["ember_public_savings_grants_test.py"],
)

# Real-Postgres test for ember_synthetic_probe public grants (PR #4065):
# public_reader can SELECT from ember_synthetic_probe (the read path the public
# tier's /api/health uses to detect probe timeouts and report service health),
# but cannot INSERT or UPDATE it (Task 1, narrowing writes to the app role).
# Unlike demo_pg_savings, there is no public_writer grant here: the writer is
# the app role (the ember-synthetic CronWorkflows jobs pod), not the public tier.
bdd_test(
    name = "ember_synthetic_probe_grants_test",
    srcs = ["ember_synthetic_probe_grants_test.py"],
)

bdd_test(
    name = "home_bdd_playwright_test",
    timeout = "long",
    srcs = [
        "//projects/monolith/home:tests/__init__.py",
        "//projects/monolith/home:tests/bdd_playwright_test.py",
        "//projects/monolith/home:tests/conftest.py",
    ],
    playwright = True,
)

bdd_test(
    name = "home_bdd_public_test",
    srcs = [
        "//projects/monolith/home:tests/__init__.py",
        "//projects/monolith/home:tests/bdd_public_test.py",
        "//projects/monolith/home:tests/conftest.py",
    ],
)

bdd_test(
    name = "chat_bdd_api_test",
    srcs = [
        "//projects/monolith/chat:tests/__init__.py",
        "//projects/monolith/chat:tests/bdd_api_test.py",
        "//projects/monolith/chat:tests/conftest.py",
    ],
)

bdd_test(
    name = "knowledge_bdd_api_test",
    srcs = [
        "//projects/monolith/knowledge:tests/__init__.py",
        "//projects/monolith/knowledge:tests/bdd_api_test.py",
        "//projects/monolith/knowledge:tests/bdd_health_kg_test.py",
        "//projects/monolith/knowledge:tests/bdd_raws_test.py",
        "//projects/monolith/knowledge:tests/bdd_raws_usage_test.py",
        "//projects/monolith/knowledge:tests/bdd_reports_test.py",
        "//projects/monolith/knowledge:tests/conftest.py",
    ],
)

bdd_test(
    name = "knowledge_bdd_playwright_test",
    timeout = "long",
    srcs = [
        "//projects/monolith/knowledge:tests/__init__.py",
        "//projects/monolith/knowledge:tests/bdd_playwright_test.py",
        "//projects/monolith/knowledge:tests/conftest.py",
    ],
    playwright = True,
)

bdd_test(
    name = "knowledge_bdd_public_test",
    srcs = [
        "//projects/monolith/knowledge:tests/__init__.py",
        "//projects/monolith/knowledge:tests/bdd_public_test.py",
        "//projects/monolith/knowledge:tests/conftest.py",
    ],
)

# BDD specs for the website snapshotter MCP tool (issue #4994). These specs
# define the tool's contract: URL validation, viewport bounds, MCP
# registration, EmberVM dispatch (mocked), and the return shape (ImageContent
# block plus SeaweedFS URL). Promoted out of future = True in T5: the
# shotter domain now exists and all 29 specs pass.
bdd_test(
    name = "shotter_bdd_mcp_tool_test",
    srcs = [
        "//projects/monolith/shotter:tests/__init__.py",
        "//projects/monolith/shotter:tests/bdd_mcp_tool_test.py",
        "//projects/monolith/shotter:tests/conftest.py",
    ],
)

py_test(
    name = "bdd_completeness_test",
    size = "small",
    srcs = [
        "//projects/monolith/app:__init__.py",
        "//projects/monolith/app:bdd_completeness_test.py",
    ],
    # The data glob hoovers up EVERY domain's tests, so each one needs an
    # explicit --ignore below or pytest collects and runs it here. A `future`
    # tag does not help: that tags the dedicated bdd_test target, while this is
    # a separate py_test that picks the file up by glob.
    data = [
        "//projects/monolith/agent:bdd_tests",
        "//projects/monolith/agent_sessions:bdd_tests",
        "//projects/monolith/app:bdd_tests",
        "//projects/monolith/artifact:bdd_tests",
        "//projects/monolith/auth:bdd_tests",
        "//projects/monolith/campsites:bdd_tests",
        "//projects/monolith/chat:bdd_tests",
        "//projects/monolith/chat_public:bdd_tests",
        "//projects/monolith/cluster:bdd_tests",
        "//projects/monolith/core:bdd_tests",
        "//projects/monolith/demos:bdd_tests",
        "//projects/monolith/dr_jobs:bdd_tests",
        "//projects/monolith/e2e:bdd_tests",
        "//projects/monolith/ember_public:bdd_tests",
        "//projects/monolith/faas:bdd_tests",
        "//projects/monolith/framework:bdd_tests",
        "//projects/monolith/goosecracker:bdd_tests",
        "//projects/monolith/grimoire:bdd_tests",
        "//projects/monolith/grimoire_chat:bdd_tests",
        "//projects/monolith/hikes:bdd_tests",
        "//projects/monolith/home:bdd_tests",
        "//projects/monolith/knowledge:bdd_tests",
        "//projects/monolith/moving:bdd_tests",
        "//projects/monolith/observability:bdd_tests",
        "//projects/monolith/sandbox:bdd_tests",
        "//projects/monolith/scheduler:bdd_tests",
        "//projects/monolith/scripts:bdd_tests",
        "//projects/monolith/semgrep_scan:bdd_tests",
        "//projects/monolith/shared:bdd_tests",
        "//projects/monolith/ships:bdd_tests",
        "//projects/monolith/shotter:bdd_tests",
        "//projects/monolith/stars:bdd_tests",
        "//projects/monolith/swarm:bdd_tests",
        "//projects/monolith/trips:bdd_tests",
        "//projects/monolith/updates:bdd_tests",
        "//projects/monolith/worldcup:bdd_tests",
    ],
    env = {"PYTEST_ADDOPTS": "--import-mode=importlib --ignore=projects/monolith/home/tests --ignore=projects/monolith/chat/tests --ignore=projects/monolith/knowledge/tests --ignore=projects/monolith/scheduler/tests --ignore=projects/monolith/agent/tests --ignore=projects/monolith/goosecracker/tests --ignore=projects/monolith/moving/tests --ignore=projects/monolith/shotter/tests"},
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
    srcs = ["//projects/monolith/agent:config_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "agent_routine_reconciliation_test",
    srcs = ["//projects/monolith/agent:routine_reconciliation_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        ":pkg_cyclegroup",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_routine_jobs_test",
    srcs = ["//projects/monolith/agent:routine_jobs_test.py"],
    imports = ["."],
    deps = [
        ":agent_sessions_admission",
        ":pkg_cyclegroup",
        ":pkg_swarm",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_notify_test",
    srcs = ["//projects/monolith/agent:notify_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

bdd_test(
    name = "agent_bdd_locks_test",
    srcs = [
        "//projects/monolith/agent:tests/__init__.py",
        "//projects/monolith/agent:tests/bdd_locks_test.py",
        "//projects/monolith/agent:tests/conftest.py",
    ],
)

bdd_test(
    name = "agent_bdd_routine_jobs_test",
    srcs = [
        "//projects/monolith/agent:tests/__init__.py",
        "//projects/monolith/agent:tests/bdd_routine_jobs_test.py",
        "//projects/monolith/agent:tests/conftest.py",
    ],
)

bdd_test(
    name = "agent_bdd_drainer_test",
    srcs = [
        "//projects/monolith/agent:tests/__init__.py",
        "//projects/monolith/agent:tests/bdd_drainer_test.py",
        "//projects/monolith/agent:tests/conftest.py",
    ],
)

bdd_test(
    name = "agent_bdd_checks_test",
    srcs = [
        "//projects/monolith/agent:tests/__init__.py",
        "//projects/monolith/agent:tests/bdd_checks_test.py",
        "//projects/monolith/agent:tests/conftest.py",
    ],
)

py_test(
    name = "goosecracker_repo_catalog_test",
    srcs = ["//projects/monolith/goosecracker:tests/repo_catalog_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "goosecracker_recipe_catalog_test",
    srcs = ["//projects/monolith/goosecracker:tests/recipe_catalog_test.py"],
    data = ["//projects/monolith/goosecracker:recipes"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "agent_sessions_store_voice_test",
    srcs = ["//projects/monolith/agent_sessions:store_voice_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_titles_test",
    srcs = ["//projects/monolith/agent_sessions:titles_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_kg_feed_test",
    srcs = ["//projects/monolith/agent_sessions:kg_feed_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_mcp_test",
    srcs = ["//projects/monolith/agent_sessions:mcp_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        "@pip//fastmcp",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_provider_quota_test",
    srcs = ["//projects/monolith/agent_sessions:provider_quota_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        ":pkg_framework",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_transport_test",
    srcs = ["//projects/monolith/agent_sessions:transport_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        "@pip//httpx",
        "@pip//opentelemetry_sdk",
        "@pip//pytest",
    ],
)

py_test(
    name = "agent_sessions_voice_ui_test",
    srcs = ["//projects/monolith/agent_sessions:voice_ui_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        # The companion routes and the voice_ui_* tools read the request
        # principal, so the test drives auth.dependencies directly.
        ":pkg_auth",
        ":pkg_core",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_swarm_decision_test",
    srcs = ["//projects/monolith/agent_sessions:swarm_decision_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        ":pkg_auth",
        ":pkg_swarm",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

semgrep_target_test(
    name = "add_visibility_field_semgrep_test",
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-stdlib-fastapi",
        "tainted-path-traversal-pillow-fastapi",
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
        "tainted-path-traversal-pillow-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
    target = ":classify_visibility_backfill",
)

# Run-by-hand backfill: re-derive trips.points from S3 image EXIF (+ optional
# KML gap route and NRCan elevation) and upsert into Postgres. Not part of the
# server image (excluded from the :main / :monolith_backend globs above).
#   bazel run //projects/monolith:trips_backfill -- run --slug <slug> ...
alias(
    name = "trips_backfill_lib",
    actual = "//projects/monolith/trips:trips_backfill_lib",
    visibility = ["//:__subpackages__"],
)

alias(
    name = "trips_backfill",
    actual = "//projects/monolith/trips:trips_backfill",
    visibility = ["//:__subpackages__"],
)

semgrep_target_test(
    name = "trips_backfill_semgrep_test",
    exclude_rules = [
        # main.py guards the SeaweedFS endpoint with a scheme check, but
        # semgrep-core ignores the inline # nosemgrep (mirrors :main). The
        # # keep stops gazelle from dropping it back to the file directive set.
        "boto3-endpoint-url-missing-scheme",  # keep
        # trips.models uses datetime fields for the migration's TIMESTAMPTZ
        # columns; semgrep-core ignores the inline # nosemgrep (mirrors :main).
        "sqlmodel-datetime-without-factory",
        "avoid-sqlalchemy-text",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-stdlib-fastapi",
        # validate_image opens in-memory bytes, not a path; false positive (mirrors :main).
        "tainted-path-traversal-pillow-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
    target = ":trips_backfill",
)

# Backfill tests dep on :trips_backfill_lib (boto3/typer/httpx orchestration),
# NOT :monolith_backend, since the backfill is excluded from the server image.
py_test(
    name = "trips_backfill_main_test",
    srcs = ["//projects/monolith/trips:backfill/main_test.py"],
    imports = ["."],
    deps = [
        ":trips_backfill_lib",
        "@pip//pytest",
    ],
)

py_test(
    name = "trips_models_test",
    srcs = ["//projects/monolith/trips:models_test.py"],
    imports = ["."],
    deps = [
        ":pkg_trips",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "trips_transform_test",
    srcs = ["//projects/monolith/trips:transform_test.py"],
    imports = ["."],
    deps = [
        ":pkg_trips",
        "@pip//pytest",
    ],
)

py_test(
    name = "trips_exif_test",
    srcs = ["//projects/monolith/trips:exif_test.py"],
    imports = ["."],
    deps = [
        ":pkg_trips",
        "@pip//pytest",
    ],
)

py_test(
    name = "trips_ingest_test",
    srcs = ["//projects/monolith/trips:ingest_test.py"],
    # Tiny committed JPEG fixtures (geotagged + no-GPS) the test reads through
    # the real exif.extract_exif path. Excluded from the server libs above so
    # the bytes never ship in the production image.
    data = ["//projects/monolith/trips:testdata"],
    imports = ["."],
    deps = [
        ":pkg_trips",
        "@pip//pytest",
    ],
)

py_test(
    name = "trips_ingest_router_test",
    srcs = ["//projects/monolith/trips:ingest_router_test.py"],
    # Same committed JPEG fixtures posted through the TestClient.
    data = ["//projects/monolith/trips:testdata"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_trips",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//python_multipart",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "trips_read_router_test",
    srcs = ["//projects/monolith/trips:read_router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_trips",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

semgrep_test(
    name = "import_boundaries_test_semgrep_test",
    srcs = ["import_boundaries_test.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-stdlib-fastapi",
        "tainted-path-traversal-pillow-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_target_test(
    name = "main_public_semgrep_test",
    exclude_rules = [
        "avoid-sqlalchemy-text",
        # knowledge/api.py search_public_chunks builds a fully parameterized
        # SQLModel select (cosine_distance/order_by/limit), no raw SQL, so the
        # taint flag is a false positive. Mirrors :main_semgrep_test's exclude.
        "generic-sql-fastapi",  # keep
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-stdlib-fastapi",
        # Same acknowledged false-positives that :main_semgrep_test excludes;
        # the pruned public binary includes the same flagged files
        # (stars/grid.py guards endpoint_url with a scheme check;
        # knowledge/profile.py has an intentional audit-date constant).
        # semgrep-core ignores inline # nosemgrep, so exclude here. # keep
        "boto3-endpoint-url-missing-scheme",  # keep
        "test-hardcoded-past-timestamp",  # keep
        "tainted-path-traversal-pillow-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
    target = ":main_public",
)

semgrep_target_test(
    name = "gen_repo_docs_manifest_semgrep_test",
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-stdlib-fastapi",
        "tainted-path-traversal-pillow-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
    target = ":gen_repo_docs_manifest",
)

semgrep_target_test(
    name = "gen_docs_manifest_semgrep_test",
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-stdlib-fastapi",
        "tainted-path-traversal-pillow-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
    target = ":gen_docs_manifest",
)

# Hand-registered (chat_public is gazelle-excluded): rolling-summary helpers
# (_format_turns, _build_messages, summarize) with mocked inference.complete.
py_test(
    name = "chat_public_summarizer_test",
    srcs = ["//projects/monolith/chat_public:summarizer_test.py"],
    imports = ["."],
    deps = [
        "//projects/monolith/public_backend:monolith_public_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

# Hand-registered (chat_public is gazelle-excluded): unit tests for the
# session lifecycle pure functions and DB helpers (hash_value, _as_utc,
# _is_expired, load_active_session, touch, record_turn, get_transcript,
# compact_if_needed). SQLite + StaticPool, no HTTP layer.
py_test(
    name = "chat_public_sessions_test",
    srcs = ["//projects/monolith/chat_public:sessions_test.py"],
    imports = ["."],
    deps = [
        "//projects/monolith/public_backend:monolith_public_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

# Hand-registered (knowledge is gazelle-excluded): pure HTTP-cache helpers
# (_as_utc, _graph_etag, _GRAPH_CACHE_CONTROL).
py_test(
    name = "knowledge_http_cache_test",
    srcs = ["//projects/monolith/knowledge:http_cache_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

# Hand-registered (knowledge is gazelle-excluded): PublicNote, PublicNoteLink,
# PublicChunk SQLModel definitions -- instantiation and SQLite round-trips.
py_test(
    name = "knowledge_public_models_test",
    srcs = ["//projects/monolith/knowledge:public_models_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

# Hand-registered (knowledge is gazelle-excluded): public knowledge graph +
# note endpoints via TestClient + SQLite session.
py_test(
    name = "knowledge_public_router_test",
    srcs = ["//projects/monolith/knowledge:public_router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        ":pkg_knowledge",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

# Hand-registered (agent_sessions is gazelle-excluded): aggregate response
# shaping, view-only queries, cache headers, conditional ETags, and failures.
py_test(
    name = "agent_sessions_public_agent_activity_router_test",
    srcs = ["//projects/monolith/agent_sessions:public_agent_activity_router_test.py"],
    imports = ["."],
    deps = [
        "//projects/monolith/public_backend:monolith_public_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
    ],
)

# Hand-registered (agent_sessions is gazelle-excluded): the public factory
# routes read only the public_api snapshot tables, with cache headers,
# conditional ETags, 404s before the first snapshot, and colon-bearing keys.
py_test(
    name = "agent_sessions_public_factory_router_test",
    srcs = ["//projects/monolith/agent_sessions:public_factory_router_test.py"],
    imports = ["."],
    deps = [
        "//projects/monolith/public_backend:monolith_public_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlalchemy",
    ],
)

# Hand-registered (trips is gazelle-excluded): SeaweedFS S3 image upload --
# missing endpoint, scheme guard, head-hit skip, auto-create bucket.
py_test(
    name = "trips_s3_test",
    srcs = ["//projects/monolith/trips:s3_test.py"],
    imports = ["."],
    deps = [
        ":pkg_trips",
        "@pip//boto3",
        "@pip//botocore",
        "@pip//pytest",
    ],
)

# Hand-registered: CERRA backfill pure helpers (_to_xyz, sun_elevation_deg).
# xarray/cfgrib/scipy are mocked via sys.modules so only numpy is needed.
py_test(
    name = "stars_backfill_cerra_test",
    srcs = [
        "//projects/monolith/stars:grid_gen/backfill_cerra.py",
        "//projects/monolith/stars:grid_gen/backfill_cerra_test.py",
    ],
    imports = ["stars/grid_gen"],
    deps = [
        "@pip//numpy",
        "@pip//pytest",
    ],
)

# Hand-registered: ERA5 climatology backfill pure helpers (stdlib-only module).
py_test(
    name = "stars_backfill_climatology_test",
    srcs = [
        "//projects/monolith/stars:grid_gen/backfill_climatology.py",
        "//projects/monolith/stars:grid_gen/backfill_climatology_test.py",
    ],
    imports = ["stars/grid_gen"],
    deps = ["@pip//pytest"],
)

semgrep_target_test(
    name = "jobs_semgrep_test",
    # :jobs scans the identical source closure as :main, so it must carry the
    # identical exclude_rules. Beyond the file-level gazelle directive, these
    # mirror main_semgrep_test's hand-kept excludes; see that target for the
    # per-rule rationale (semgrep-core does not honour inline # nosemgrep).
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "boto3-endpoint-url-missing-scheme",  # keep
        "generic-sql-fastapi",  # keep
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
        "test-hardcoded-past-timestamp",  # keep
    ],
    lockfiles = ["//bazel/requirements:all.txt"],
    rules = ["//bazel/semgrep/rules:python_rules"],
    sca_rules = ["//bazel/semgrep/rules:sca_python_rules"],
    target = ":jobs",
)

semgrep_target_test(
    name = "gen_orchestrator_bundle_semgrep_test",
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
    target = ":gen_orchestrator_bundle",
)

semgrep_target_test(
    name = "main_domain_semgrep_test",
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
    target = ":main_domain",
)

semgrep_target_test(
    name = "progress_semgrep_test",
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
    target = ":progress",
)

semgrep_test(
    name = "public_library_image_digest_guard_test_semgrep_test",
    srcs = ["public_library_image_digest_guard_test.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_target_test(
    name = "gen_posts_manifest_semgrep_test",
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
    target = ":gen_posts_manifest",
)

semgrep_test(
    name = "public_cilium_scoped_egress_guard_test_semgrep_test",
    srcs = ["public_cilium_scoped_egress_guard_test.py"],
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
    name = "github_repo_slug_test_semgrep_test",
    srcs = ["github_repo_slug_test.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)

semgrep_target_test(
    name = "main_agents_semgrep_test",
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
    target = ":main_agents",
)

# Static boundary contract for the inactive GKE recovery manifests.
py_test(
    name = "recovery_network_test",
    srcs = ["recovery_network_test.py"],
    data = [
        "//projects/embervm/dev/deploy:recovery_values",
        "//projects/monolith/dev/deploy:recovery_support_manifests",
    ],
    env = {
        "EMBER_RECOVERY_VALUES": "$(rootpath //projects/embervm/dev/deploy:recovery_values)",
    },
    deps = [
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

semgrep_test(
    name = "recovery_network_test_semgrep_test",
    srcs = ["recovery_network_test.py"],
    exclude_rules = [
        "avoid-sqlalchemy-text",
        "sqlmodel-datetime-without-factory",
        "tainted-fastapi-http-request-httpx",
        "tainted-path-traversal-pillow-fastapi",
        "tainted-path-traversal-stdlib-fastapi",
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
)
