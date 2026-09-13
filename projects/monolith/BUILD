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

package_group(
    name = "private_backend_consumers",
    packages = [
        "-//projects/monolith/public_backend",
        "//projects/monolith",
        "//projects/monolith/...",
    ],
)

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

# Shared source closure for the monolith binaries. Both the API server
# (:main / :image) and the batch-jobs entrypoint (:jobs / :jobs_image) glob the
# same tree so their image dependency layers are byte-identical: Bazel caches
# them and the OCI registry dedupes the blobs, so the jobs image costs only its
# thin launcher layer to build and push. Keep them sharing this variable rather
# than maintaining two globs that could silently drift apart.
py_venv_binary(
    name = "main",
    srcs = ["//projects/monolith/app:main.py"],
    data = [
        # ratings.py reads this committed Elo snapshot at runtime via
        # Path(__file__).parent/"ratings"/"elo_2026.json"; the "worldcup/**/*.py"
        # glob does not capture the .json, so ship it explicitly into runfiles.
        "//projects/monolith/worldcup:runtime_data",
        # campsites.client.load_catalog reads this committed static catalog at
        # runtime via importlib.resources.files("campsites")/"catalog.json"; the
        # "campsites/**/*.py" glob does not capture the .json, so ship it into
        # runfiles explicitly (mirrors the worldcup Elo snapshot above).
        "//projects/monolith/campsites:runtime_data",
        # directives.get_active reads this committed base directive at runtime
        # via Path(__file__).parent / "directive_seed.md" (ADR 035 phase 5); the
        # "chat/**/*.py" glob does not capture the .md, so ship it explicitly.
        "//projects/monolith/chat:runtime_data",
        # orchestrator.load_bundle reads this committed baked context bundle at
        # runtime via Path(__file__).with_name("orchestrator_bundle.md") (ADR
        # 036); the "chat/**/*.py" glob does not capture the .md, so ship it
        # explicitly or every escalation fails open with FileNotFoundError.
        # loadtest_corpus.load_corpus reads these committed .sample files at
        # runtime via Path(__file__).parent; the "demos/**/*.py" glob does not
        # capture non-.py data, so ship them explicitly into runfiles (mirrors
        # the worldcup Elo snapshot and campsites catalog above).
        ":frontend_dist",
    ] + ["//projects/monolith/demos:loadtest_samples"],
    imports = ["."],  # keep
    main = "//projects/monolith/app:main.py",
    visibility = ["//:__subpackages__"],
    deps = ["//projects/monolith:monolith_backend"],  # keep
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
    srcs = ["//projects/monolith/knowledge:repo_docs_manifest"],
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
    srcs = ["//projects/monolith/app:jobs_main.py"],
    data = [
        "//projects/monolith/worldcup:runtime_data",
        # The campsites-refresh job (campsites.jobs.refresh_handler) reads this
        # static catalog from runfiles via campsites.client.load_catalog, so it
        # must ship in the jobs image too (data on :main does not propagate to
        # this separate binary).
        "//projects/monolith/campsites:runtime_data",
    ],
    imports = ["."],  # keep
    main = "//projects/monolith/app:jobs_main.py",
    visibility = ["//:__subpackages__"],
    # typer is the jobs entrypoint's CLI framework but is NOT a serving dep, so
    # it is absent from :monolith_backend; the binary must depend on it directly
    # or the jobs image fails at startup with `ModuleNotFoundError: typer`.
    deps = [
        "//projects/monolith:monolith_backend",
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
    srcs = ["//projects/monolith/app:progress_main.py"],
    imports = ["."],  # keep
    main = "//projects/monolith/app:progress_main.py",
    visibility = ["//:__subpackages__"],
    deps = ["//projects/monolith:monolith_backend"],  # keep
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

py_library(
    name = "pkg_cyclegroup",
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        "//projects/monolith/agent",
        "//projects/monolith/agent_sessions:agent_sessions_admission",
        "//projects/monolith/artifact",
        "//projects/monolith/chat",
        "//projects/monolith/cluster",
        "//projects/monolith/core",
        "//projects/monolith/framework",
        "//projects/monolith/goosecracker",
        "//projects/monolith/home",
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

py_library(
    name = "monolith_backend",
    # entities.yaml is 2.5 KB and deliberately ships as runfiles data, unlike
    # the multi-megabyte ndjson manifest isolated in its own image layer.
    visibility = ["//:__subpackages__"],
    deps = [
        "//projects/monolith/agent_sessions",
        "//projects/monolith/app",
        "//projects/monolith/auth",
        "//projects/monolith/core",
        "//projects/monolith/framework",
        "//projects/monolith/artifact",
        "//projects/monolith/chat_public",
        "//projects/monolith/cluster",
        "//projects/monolith/demos",
        "//projects/monolith/ember_public",
        "//projects/monolith/knowledge",
        "//projects/monolith/observability",
        "//projects/monolith/sandbox",
        "//projects/monolith/shotter",
        "//projects/monolith/scheduler",
        "//projects/monolith/semgrep_scan",
        "//projects/monolith/shared",
        "//projects/monolith/ships",
        "//projects/monolith/hikes",
        "//projects/monolith/moving",
        "//projects/monolith/stars",
        "//projects/monolith/trips",
        "//projects/monolith/dr_jobs",
        "//projects/monolith/worldcup",
        "//projects/monolith/campsites",
        "//projects/monolith/grimoire",
        "//projects/monolith/grimoire_chat",
        "//projects/monolith/faas",
        "//projects/monolith/swarm",
        "//projects/monolith/updates",
        ":pkg_cyclegroup",
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

# Compatibility label used by bdd_test.bzl. The testing source target is owned
# by the shared package.
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
    main = "app/main.py",
    main_label = "//projects/monolith/app:main.py",
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
    main = "app/jobs_main.py",
    main_label = "//projects/monolith/app:jobs_main.py",
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
    main = "app/progress_main.py",
    main_label = "//projects/monolith/app:progress_main.py",
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
    main = "app/agents_main.py",
    main_label = "//projects/monolith/app:agents_main.py",
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
# runs app/agents_main.py. It carries only the knowledge catalogue plus the
# auth, core, framework, shared, and app support code needed to serve it. Every
# unrelated domain and every private knowledge, home, cluster, and app path is
# excluded from the source glob below. app/main_agents_imports_test.py imports
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
    srcs = ["//projects/monolith/app:agents_main.py"],
    imports = ["."],  # keep
    main = "//projects/monolith/app:agents_main.py",
    visibility = ["//:__subpackages__"],
    deps = ["//projects/monolith:monolith_agents_backend"],  # keep
)

alias(
    name = "monolith_public_backend",
    actual = "//projects/monolith/public_backend:monolith_public_backend",
    visibility = ["//:__subpackages__"],
)

py_venv_binary(
    name = "main_public",
    srcs = ["//projects/monolith/app:main_public.py"],
    imports = ["."],  # keep
    main = "//projects/monolith/app:main_public.py",
    visibility = ["//:__subpackages__"],
    deps = ["//projects/monolith:monolith_public_backend"],  # keep
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
    main = "app/main_public.py",
    main_label = "//projects/monolith/app:main_public.py",
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
    srcs = ["//projects/monolith/app:main_domain.py"],
    data = [
        # Same runtime data files as :main (any domain may be selected at
        # startup); only the static frontend bundle is omitted, because the
        # domain profile never serves it (framework: static_frontend=False).
        "//projects/monolith/campsites:runtime_data",
        "//projects/monolith/chat:runtime_data",
        "//projects/monolith/worldcup:runtime_data",
    ] + ["//projects/monolith/demos:loadtest_samples"],
    imports = ["."],  # keep
    main = "//projects/monolith/app:main_domain.py",
    visibility = ["//:__subpackages__"],
    deps = ["//projects/monolith:monolith_backend"],  # keep
)

monolith_domain_images(
    name = "domain_images",
    binary = ":main_domain",
    config_test_domain = "hikes",
    domains = MONOLITH_DOMAINS,
    extra_env = {"REPO_DOCS_MANIFEST_PATH": "/etc/monolith/repo_docs_manifest.ndjson"},
    extra_tars = [":repo_docs_manifest_layer"],
    main = "app/main_domain.py",
    main_label = "//projects/monolith/app:main_domain.py",
)

# Reads every domain source file from disk (via rglob) to enforce that domains
# only import each other through <domain>/api.py, so the scanned files must be
# materialised as data (the same pattern as bdd_completeness_test).
py_test(
    name = "import_boundaries_test",
    srcs = ["import_boundaries_test.py"],
    data = [
        "//projects/monolith/agent:production_sources",
        "//projects/monolith/agent_sessions:production_sources",
        "//projects/monolith/artifact:production_sources",
        "//projects/monolith/auth:production_sources",
        "//projects/monolith/campsites:production_sources",
        "//projects/monolith/chat:production_sources",
        "//projects/monolith/chat_public:production_sources",
        "//projects/monolith/dr_jobs:production_sources",
        "//projects/monolith/goosecracker:production_sources",
        "//projects/monolith/grimoire_chat:production_sources",
        "//projects/monolith/hikes:production_sources",
        "//projects/monolith/home:production_sources",
        "//projects/monolith/knowledge:production_sources",
        "//projects/monolith/moving:production_sources",
        "//projects/monolith/scheduler:production_sources",
        "//projects/monolith/ships:production_sources",
        "//projects/monolith/stars:production_sources",
        "//projects/monolith/trips:production_sources",
        "//projects/monolith/worldcup:production_sources",
    ],
    imports = ["."],
    deps = ["@pip//pytest"],
)

# The sources are `data`, not just `srcs`: this test WALKS the tree rather than
# importing it, and a bazel test only sees its own runfiles. Without the glob the
# walk finds nothing, the violations list is empty, and the test passes while
# guarding nothing. The test asserts a floor on the file count for the same
# reason, so a future glob that silently stops matching fails loudly.
py_test(
    name = "github_repo_slug_test",
    srcs = ["github_repo_slug_test.py"],
    data = [
        "//projects/monolith/agent:non_test_python_sources",
        "//projects/monolith/agent_sessions:non_test_python_sources",
        "//projects/monolith/app:non_test_python_sources",
        "//projects/monolith/artifact:non_test_python_sources",
        "//projects/monolith/auth:non_test_python_sources",
        "//projects/monolith/campsites:non_test_python_sources",
        "//projects/monolith/chat:non_test_python_sources",
        "//projects/monolith/chat_public:non_test_python_sources",
        "//projects/monolith/cluster:non_test_python_sources",
        "//projects/monolith/core:non_test_python_sources",
        "//projects/monolith/demos:non_test_python_sources",
        "//projects/monolith/dr_jobs:non_test_python_sources",
        "//projects/monolith/e2e:non_test_python_sources",
        "//projects/monolith/ember_public:non_test_python_sources",
        "//projects/monolith/faas:non_test_python_sources",
        "//projects/monolith/framework:non_test_python_sources",
        "//projects/monolith/goosecracker:non_test_python_sources",
        "//projects/monolith/grimoire:non_test_python_sources",
        "//projects/monolith/grimoire_chat:non_test_python_sources",
        "//projects/monolith/hikes:non_test_python_sources",
        "//projects/monolith/home:non_test_python_sources",
        "//projects/monolith/knowledge:non_test_python_sources",
        "//projects/monolith/moving:non_test_python_sources",
        "//projects/monolith/notes:non_test_python_sources",
        "//projects/monolith/observability:non_test_python_sources",
        "//projects/monolith/sandbox:non_test_python_sources",
        "//projects/monolith/scheduler:non_test_python_sources",
        "//projects/monolith/scripts:non_test_python_sources",
        "//projects/monolith/semgrep_scan:non_test_python_sources",
        "//projects/monolith/shared:non_test_python_sources",
        "//projects/monolith/ships:non_test_python_sources",
        "//projects/monolith/shotter:non_test_python_sources",
        "//projects/monolith/stars:non_test_python_sources",
        "//projects/monolith/swarm:non_test_python_sources",
        "//projects/monolith/trips:non_test_python_sources",
        "//projects/monolith/updates:non_test_python_sources",
        "//projects/monolith/worldcup:non_test_python_sources",
    ],
    imports = ["."],
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

sh_binary(
    name = "generate-routes",
    srcs = ["generate-routes.sh"],
    visibility = ["//bazel/tools/format:__pkg__"],
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

alias(
    name = "trips_backfill",
    actual = "//projects/monolith/trips:trips_backfill",
    visibility = ["//:__subpackages__"],
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
