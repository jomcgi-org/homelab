# gazelle:exclude agent
# gazelle:exclude agent_sessions
# gazelle:exclude auth
# gazelle:exclude artifact
# gazelle:exclude chat
# gazelle:exclude chat_public
# gazelle:exclude cluster
# gazelle:exclude app
# gazelle:exclude core
# gazelle:exclude demos
# gazelle:exclude ember_public
# gazelle:exclude e2e
# gazelle:exclude framework
# gazelle:exclude swarm
# gazelle:exclude knowledge
# gazelle:exclude notes
# gazelle:exclude scripts
# gazelle:exclude home
# gazelle:exclude sandbox
# gazelle:exclude scheduler
# gazelle:exclude semgrep_scan
# gazelle:exclude goosecracker
# gazelle:exclude shared
# gazelle:exclude ships
# gazelle:exclude hikes
# gazelle:exclude moving
# gazelle:exclude stars
# gazelle:exclude trips
# gazelle:exclude dr_jobs
# gazelle:exclude worldcup
# gazelle:exclude campsites
# gazelle:exclude grimoire
# gazelle:exclude grimoire_chat
# gazelle:exclude faas
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
    srcs = glob(["*/config.py"]),
    visibility = ["//projects/monolith/chart:__pkg__"],
)

# Shared source closure for the monolith binaries. Both the API server
# (:main / :image) and the batch-jobs entrypoint (:jobs / :jobs_image) glob the
# same tree so their image dependency layers are byte-identical: Bazel caches
# them and the OCI registry dedupes the blobs, so the jobs image costs only its
# thin launcher layer to build and push. Keep them sharing this variable rather
# than maintaining two globs that could silently drift apart.
_BACKEND_SRCS = glob(
    [
        "agent/**/*.py",
        "agent_sessions/**/*.py",
        "app/**/*.py",
        "auth/**/*.py",
        "core/**/*.py",
        "framework/**/*.py",
        "artifact/**/*.py",
        "chat/**/*.py",
        "chat_public/**/*.py",
        "cluster/**/*.py",
        "demos/**/*.py",
        "ember_public/**/*.py",
        "goosecracker/**/*.py",
        "knowledge/**/*.py",
        "home/**/*.py",
        "sandbox/**/*.py",
        "scheduler/**/*.py",
        "semgrep_scan/**/*.py",
        "shared/**/*.py",
        "ships/**/*.py",
        "hikes/**/*.py",
        "moving/**/*.py",
        "stars/**/*.py",
        "trips/**/*.py",
        "dr_jobs/**/*.py",
        "worldcup/**/*.py",
        "campsites/**/*.py",
        "grimoire/**/*.py",
        "grimoire_chat/**/*.py",
        "faas/**/*.py",
        "swarm/**/*.py",
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
        # Offline grid generator: run by hand, not part of the runtime image.
        "stars/grid_gen/**",
        # Run-by-hand S3->Postgres backfill, not part of the runtime image.
        "trips/backfill/**",
    ],
)

py_venv_binary(
    name = "main",
    srcs = _BACKEND_SRCS,  # keep
    data = [
        # ratings.py reads this committed Elo snapshot at runtime via
        # Path(__file__).parent/"ratings"/"elo_2026.json"; the "worldcup/**/*.py"
        # glob does not capture the .json, so ship it explicitly into runfiles.
        "worldcup/ratings/elo_2026.json",
        # campsites.client.load_catalog reads this committed static catalog at
        # runtime via importlib.resources.files("campsites")/"catalog.json"; the
        # "campsites/**/*.py" glob does not capture the .json, so ship it into
        # runfiles explicitly (mirrors the worldcup Elo snapshot above).
        "campsites/catalog.json",
        # directives.get_active reads this committed base directive at runtime
        # via Path(__file__).parent / "directive_seed.md" (ADR 035 phase 5); the
        # "chat/**/*.py" glob does not capture the .md, so ship it explicitly.
        "chat/directive_seed.md",
        # orchestrator.load_bundle reads this committed baked context bundle at
        # runtime via Path(__file__).with_name("orchestrator_bundle.md") (ADR
        # 036); the "chat/**/*.py" glob does not capture the .md, so ship it
        # explicitly or every escalation fails open with FileNotFoundError.
        "chat/orchestrator_bundle.md",
        "knowledge/repo_docs_manifest.ndjson",
        # loadtest_corpus.load_corpus reads these committed .sample files at
        # runtime via Path(__file__).parent; the "demos/**/*.py" glob does not
        # capture non-.py data, so ship them explicitly into runfiles (mirrors
        # the worldcup Elo snapshot and campsites catalog above).
        ":frontend_dist",
    ] + glob(["demos/loadtest_corpus/**/*.sample"]),
    imports = ["."],  # keep
    main = "app/main.py",
    visibility = ["//:__subpackages__"],
    deps = [":monolith_backend"],  # keep
)

# Batch-jobs entrypoint: same source closure and deps as :main, but launches
# app/jobs_main.py (a Typer CLI) instead of the FastAPI server. Built into its
# own image (:jobs_image) so an ephemeral pod (e.g. an Argo Workflow) can run a
# single job to completion off the API pod. Only the worldcup Elo snapshot is
# shipped as data; the jobs entrypoint never serves the frontend, so the static
# bundle is omitted. The repo-docs manifest IS needed: the
# knowledge.repo_docs_reconcile job (knowledge-repo-docs-reconcile subcommand)
# reads it from runfiles, so ship it here too (it is data on :main, which does
# not propagate to this separate binary).
py_venv_binary(
    name = "jobs",
    srcs = _BACKEND_SRCS,  # keep
    data = [
        "knowledge/repo_docs_manifest.ndjson",
        "worldcup/ratings/elo_2026.json",
        # The campsites-refresh job (campsites.jobs.refresh_handler) reads this
        # static catalog from runfiles via campsites.client.load_catalog, so it
        # must ship in the jobs image too (data on :main does not propagate to
        # this separate binary).
        "campsites/catalog.json",
    ],
    imports = ["."],  # keep
    main = "app/jobs_main.py",
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
    srcs = _BACKEND_SRCS,  # keep
    imports = ["."],  # keep
    main = "app/progress_main.py",
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

# Stdlib-only generator: walks the repo tree (under BUILD_WORKSPACE_DIRECTORY
# when run via `bazel run`) and writes the committed repo-docs manifest NDJSON.
# Regenerates projects/monolith/knowledge/repo_docs_manifest.ndjson. Run via
# `bazel run //projects/monolith:gen_repo_docs_manifest`; the manifest staleness
# check in bazel/images/validate-generate-scripts.sh runs it in CI. No `imports`
# attr: it is a standalone stdlib script (like add_visibility_field), and
# imports = ["."] would repoint the venv main and break `bazel run`.
py_venv_binary(
    name = "gen_repo_docs_manifest",
    main = "knowledge/tools/gen_repo_docs_manifest.py",
    visibility = ["//:__subpackages__"],
)

# Stdlib-only generator for the PUBLIC docs manifest consumed by the SvelteKit
# /docs route. Globs the public-allowlisted repo docs (top-level docs/*.md plus
# the docs/decisions/** ADR tree) and writes the committed
# projects/monolith/frontend/src/lib/public/docs/docs-manifest.json. Run via
# `bazel run //projects/monolith:gen_docs_manifest`; freshness is enforced by the
# manifest staleness check in bazel/images/validate-generate-scripts.sh. Same no
# `imports` attr rationale as gen_repo_docs_manifest above.
py_venv_binary(
    name = "gen_docs_manifest",
    main = "knowledge/tools/gen_docs_manifest.py",
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
    main = "knowledge/tools/gen_orchestrator_bundle.py",
    visibility = ["//:__subpackages__"],
)

# Per-package libraries. Splitting :monolith_backend by subtree means a
# change under grimoire/ invalidates grimoire's tests, not all 354. The
# The packages in the agent/chat/home import cycle share one
# target because Bazel deps must be acyclic; everything else is a DAG.
py_library(
    name = "pkg_auth",
    srcs = glob(
        [
            "auth/**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
        ],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        "@pip//fastapi",
        "@pip//httpx",
        # PyJWT is currently locked transitively through mcp and semgrep.
        "@pip//pyjwt",
        "@pip//starlette",
    ],
)

py_library(
    name = "pkg_core",
    srcs = glob(
        [
            "core/**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
        ],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        "@pip//certifi",
        "@pip//fastapi",
        "@pip//fastmcp",
        "@pip//httpx",
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

py_library(
    name = "pkg_shared",
    srcs = glob(
        [
            "shared/**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
            "shared/testing/**/*.py",
        ],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        "@pip//certifi",
        "@pip//fastapi",
        "@pip//httpx",
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

py_library(
    name = "pkg_framework",
    srcs = glob(
        [
            "framework/**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
        ],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        ":pkg_auth",
        ":pkg_core",
        "@pip//certifi",
        "@pip//fastapi",
        "@pip//httpx",
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

py_library(
    name = "pkg_artifact",
    srcs = glob(
        [
            "artifact/**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
        ],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        ":pkg_framework",
        "@pip//boto3",
        "@pip//certifi",
        "@pip//fastapi",
        "@pip//httpx",
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

py_library(
    name = "pkg_scheduler",
    srcs = glob(
        [
            "scheduler/**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
        ],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        ":pkg_core",
        ":pkg_framework",
        "@pip//certifi",
        "@pip//fastapi",
        "@pip//httpx",
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

py_library(
    name = "pkg_cluster",
    srcs = glob(
        [
            "cluster/**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
        ],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        ":pkg_core",
        ":pkg_framework",
        "@pip//certifi",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//kubernetes_asyncio",
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

py_library(
    name = "pkg_trips",
    srcs = glob(
        [
            "trips/**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
            "trips/backfill/**",
        ],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        ":pkg_core",
        ":pkg_framework",
        "@pip//boto3",
        "@pip//certifi",
        "@pip//defusedxml",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//opentelemetry_exporter_otlp_proto_http",
        "@pip//opentelemetry_instrumentation_fastapi",
        "@pip//opentelemetry_instrumentation_httpx",
        "@pip//opentelemetry_sdk",
        "@pip//pgvector",
        "@pip//pillow",
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

py_library(
    name = "pkg_faas",
    srcs = glob(
        [
            "faas/**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
        ],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        ":pkg_core",
        ":pkg_framework",
        ":pkg_shared",
        "@pip//boto3",
        "@pip//certifi",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//kubernetes_asyncio",
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

py_library(
    name = "pkg_sandbox",
    srcs = glob(
        [
            "sandbox/**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
        ],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        ":pkg_core",
        ":pkg_framework",
        ":pkg_shared",
        "@pip//certifi",
        "@pip//fastapi",
        "@pip//httpx",
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

py_library(
    name = "pkg_semgrep_scan",
    srcs = glob(
        [
            "semgrep_scan/**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
        ],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        ":pkg_core",
        ":pkg_framework",
        ":pkg_shared",
        "@pip//certifi",
        "@pip//fastapi",
        "@pip//httpx",
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
        "@pip//tzdata",
        "@pip//uvicorn",
    ],
)

py_library(
    name = "pkg_campsites",
    srcs = glob(
        [
            "campsites/**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
        ],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        ":pkg_core",
        ":pkg_framework",
        ":pkg_scheduler",
        "@pip//certifi",
        "@pip//curl_cffi",
        "@pip//fastapi",
        "@pip//httpx",
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

py_library(
    name = "pkg_ships",
    srcs = glob(
        [
            "ships/**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
        ],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        ":pkg_core",
        ":pkg_framework",
        ":pkg_scheduler",
        "@pip//certifi",
        "@pip//fastapi",
        "@pip//httpx",
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
        "@pip//websockets",
    ],
)

py_library(
    name = "pkg_worldcup",
    srcs = glob(
        [
            "worldcup/**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
        ],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        ":pkg_core",
        ":pkg_framework",
        ":pkg_scheduler",
        "@pip//certifi",
        "@pip//fastapi",
        "@pip//httpx",
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

py_library(
    name = "pkg_knowledge",
    srcs = glob(
        [
            "knowledge/**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
        ],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        ":pkg_core",
        ":pkg_framework",
        ":pkg_scheduler",
        ":pkg_shared",
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

py_library(
    name = "pkg_hikes",
    srcs = glob(
        [
            "hikes/**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
        ],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        ":pkg_core",
        ":pkg_framework",
        ":pkg_scheduler",
        ":pkg_shared",
        "@pip//beautifulsoup4",
        "@pip//certifi",
        "@pip//fastapi",
        "@pip//httpx",
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
        "@pip//timelength",
        "@pip//tzdata",
        "@pip//uvicorn",
    ],
)

py_library(
    name = "pkg_moving",
    srcs = glob(
        [
            "moving/**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
        ],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        ":pkg_core",
        ":pkg_framework",
        "@pip//fastapi",
        "@pip//pydantic",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_library(
    name = "pkg_stars",
    srcs = glob(
        [
            "stars/**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
            "stars/grid_gen/**",
        ],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        ":pkg_core",
        ":pkg_framework",
        ":pkg_scheduler",
        ":pkg_shared",
        "@pip//astral",
        "@pip//boto3",
        "@pip//certifi",
        "@pip//fastapi",
        "@pip//httpx",
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

py_library(
    name = "pkg_chat_public",
    srcs = glob(
        [
            "chat_public/**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
        ],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        ":pkg_core",
        ":pkg_framework",
        ":pkg_knowledge",
        ":pkg_shared",
        "@pip//certifi",
        "@pip//fastapi",
        "@pip//httpx",
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

py_library(
    name = "pkg_grimoire",
    srcs = glob(
        [
            "grimoire/**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
        ],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        ":pkg_core",
        ":pkg_framework",
        ":pkg_knowledge",
        ":pkg_scheduler",
        ":pkg_shared",
        "@pip//boto3",
        "@pip//certifi",
        "@pip//fastapi",
        "@pip//httpx",
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

py_library(
    name = "pkg_grimoire_chat",
    srcs = glob(
        [
            "grimoire_chat/**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
        ],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        ":pkg_core",
        ":pkg_framework",
        ":pkg_grimoire",
        ":pkg_knowledge",
        "@pip//certifi",
        "@pip//fastapi",
        "@pip//httpx",
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

py_library(
    name = "pkg_ember_public",
    srcs = glob(
        [
            "ember_public/**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
        ],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        ":pkg_chat_public",
        ":pkg_core",
        ":pkg_faas",
        ":pkg_framework",
        ":pkg_semgrep_scan",
        ":pkg_shared",
        "@pip//certifi",
        "@pip//fastapi",
        "@pip//httpx",
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

py_library(
    name = "pkg_cyclegroup",
    srcs = glob(
        [
            "agent/**/*.py",
            "chat/**/*.py",
            "goosecracker/**/*.py",
            "home/**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
        ],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        ":pkg_artifact",
        ":pkg_cluster",
        ":pkg_core",
        ":pkg_framework",
        ":pkg_knowledge",
        ":pkg_sandbox",
        ":pkg_scheduler",
        ":pkg_shared",
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
    name = "agent_sessions_rationale",
    srcs = ["agent_sessions/rationale.py"],
    imports = ["."],
    visibility = ["//:__subpackages__"],
)

py_library(
    name = "pkg_agent_sessions",
    srcs = glob(
        [
            "agent_sessions/**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
            "agent_sessions/rationale.py",
        ],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        ":agent_sessions_rationale",
        ":pkg_core",
        ":pkg_cyclegroup",
        ":pkg_faas",
        ":pkg_framework",
        ":pkg_shared",
        "@pip//certifi",
        "@pip//fastapi",
        "@pip//httpx",
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

py_library(
    name = "swarm_rationale",
    srcs = ["swarm/rationale.py"],
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [":agent_sessions_rationale"],
)

py_library(
    name = "pkg_swarm",
    srcs = glob(
        ["swarm/**/*.py"],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
            "swarm/rationale.py",
        ],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        ":agent_sessions_rationale",
        ":pkg_agent_sessions",
        ":pkg_core",
        ":pkg_framework",
        "@pip//certifi",
        "@pip//dbos",
        "@pip//fastapi",
        "@pip//httpx",
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

py_library(
    name = "pkg_dr_jobs",
    srcs = glob(
        [
            "dr_jobs/**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
        ],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        ":pkg_core",
        ":pkg_cyclegroup",
        ":pkg_framework",
        ":pkg_scheduler",
        "@pip//certifi",
        "@pip//fastapi",
        "@pip//httpx",
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

py_library(
    name = "pkg_demos",
    srcs = glob(
        [
            "demos/**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
        ],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        ":pkg_cluster",
        ":pkg_core",
        ":pkg_cyclegroup",
        ":pkg_ember_public",
        ":pkg_framework",
        ":pkg_sandbox",
        ":pkg_semgrep_scan",
        ":pkg_shared",
        "@pip//certifi",
        "@pip//fastapi",
        "@pip//httpx",
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

py_library(
    name = "monolith_backend",
    srcs = glob(
        [
            "agent/**/*.py",
            "agent_sessions/**/*.py",
            "app/**/*.py",
            "auth/**/*.py",
            "core/**/*.py",
            "framework/**/*.py",
            "artifact/**/*.py",
            "chat/**/*.py",
            "chat_public/**/*.py",
            "cluster/**/*.py",
            "demos/**/*.py",
            "ember_public/**/*.py",
            "goosecracker/**/*.py",
            "knowledge/**/*.py",
            "home/**/*.py",
            "sandbox/**/*.py",
            "scheduler/**/*.py",
            "semgrep_scan/**/*.py",
            "shared/**/*.py",
            "ships/**/*.py",
            "hikes/**/*.py",
            "moving/**/*.py",
            "stars/**/*.py",
            "trips/**/*.py",
            "dr_jobs/**/*.py",
            "worldcup/**/*.py",
            "campsites/**/*.py",
            "grimoire/**/*.py",
            "grimoire_chat/**/*.py",
            "faas/**/*.py",
            "swarm/**/*.py",
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
            # Offline grid generator: run by hand, not part of the runtime image.
            "stars/grid_gen/**",
            # Run-by-hand S3->Postgres backfill (boto3/typer/httpx orchestration)
            # stays out of the runtime image; the pure exif.py/transform.py
            # modules live at trips/ level and DO ship for the live ingest +
            # SSR read paths.
            "trips/backfill/**",
        ],
    ),  # keep
    visibility = ["//:__subpackages__"],
    deps = [
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
        "@pip//httpx",
        "@pip//icalendar",
        "@pip//kubernetes_asyncio",
        "@pip//lxml_html_clean",  # explicit: overrides.txt strips lxml[html-clean] extra
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
    repository = "ghcr.io/jomcgi/homelab/projects/monolith/backend",
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
    },
    main = "app/jobs_main.py",
    repository = "ghcr.io/jomcgi/homelab/projects/monolith/jobs",
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
    repository = "ghcr.io/jomcgi/homelab/projects/monolith/progress",
)

# ---------------------------------------------------------------------------
# Public service (ADR 004 Phase 5): a pruned, read-only binary/image that runs
# app/main_public.py. It deliberately excludes every private write-path domain
# (agent/chat/scheduler/trips), the private entrypoints (app/main.py,
# app/mcp_app.py), the private knowledge routers/write internals, and the
# private home/observability writer + ClickHouse paths. Those modules are
# PROVEN absent from app.main_public's runtime closure by
# app/main_public_imports_test.py, so excluding their FILES cannot break the
# public binary. Everything else (knowledge read API/store/models/public
# routers, home read router/config/models, ships/stars/hikes/dr_jobs/shared)
# stays in full. The pip deps mirror :monolith_backend wholesale: a missing
# wheel would crash the public binary at startup, so trimming is a follow-up.
_PUBLIC_PRUNE_EXCLUDE = [
    # Standard excludes (mirror :monolith_backend / :main).
    "**/*_test.py",
    # Semgrep: the public tier ships only semgrep_scan/client.py (+ __init__)
    # for the ember demo; the MCP tool, App reporting, PR router, full-scan
    # and perf surfaces are private-only.
    "semgrep_scan/mcp.py",
    "semgrep_scan/module.py",
    "semgrep_scan/report.py",
    "semgrep_scan/router.py",
    "semgrep_scan/cohorts.py",
    "semgrep_scan/perf_*.py",
    "**/test_*.py",
    "*/tests/**/*.py",
    "shared/testing/**/*.py",
    "stars/grid_gen/**",
    "trips/backfill/**",
    # Whole private domains.
    "agent/**",
    "chat/**",
    "cluster/**",
    "goosecracker/**",
    "scheduler/**",
    # Trips: the read path (models.py + read_router.py + __init__.py) ships in
    # the public image; the write/heavy path stays out (pillow/boto3/defusedxml).
    "trips/ingest_router.py",
    "trips/ingest.py",
    "trips/s3.py",
    "trips/exif.py",
    "trips/transform.py",
    # Worldcup: only the read path (models.py + router.py + __init__.py) ships in
    # the public image. __init__.register_public imports worldcup.router, and
    # router imports worldcup.models; neither pulls the sim/poller graph. The
    # simulate/poll-only modules (jobs/client/sim/outcome/ratings) and the Elo
    # snapshot data stay out (httpx/scheduler/sim closure is private-only).
    "worldcup/jobs.py",
    "worldcup/client.py",
    "worldcup/sim.py",
    "worldcup/outcome.py",
    "worldcup/ratings.py",
    # The "*.py" include glob never captures the JSON; listed for defence in
    # depth (the public binary also omits it from `data`).
    "worldcup/ratings/elo_2026.json",
    # Campsites: only the read path (models.py + router.py + __init__.py) ships in
    # the public image. The scraper and weather-fetch modules (jobs/client/weather)
    # pull curl_cffi/httpx and stay out of the public binary, as does the run-by-hand
    # catalog generator (gen_catalog.py).
    "campsites/jobs.py",
    "campsites/client.py",
    "campsites/weather.py",
    "campsites/gen_catalog.py",
    # Private entrypoint + MCP app.
    "app/main.py",
    "app/mcp_app.py",
    # Private module registry (imports chat/agent/demos/...) and the
    # single-domain entrypoint: never part of the public composition.
    "app/modules_private.py",
    "app/main_domain.py",
    # Batch-jobs entrypoint: runs as :jobs_image, never in the public binary.
    "app/jobs_main.py",
    # Ember synthetic prober and private trigger: run only in the private API
    # and jobs images. The public tier ships the READ side (synthetic.py +
    # synthetic_models.py, which health.py needs to answer /api/health) but must
    # not carry the prober or internal endpoint that drives the workloads, same split as
    # worldcup/jobs.py and campsites/jobs.py.
    "ember_public/synthetic_probe.py",
    "ember_public/synthetic_router.py",
    # Private knowledge routers / write paths / heavy maintenance internals.
    "knowledge/router.py",
    "knowledge/tasks_router.py",
    "knowledge/gaps.py",
    "knowledge/ingest_queue.py",
    "knowledge/mcp.py",
    "knowledge/service.py",
    "knowledge/layout.py",
    # Private home scheduling + observability writer/ClickHouse paths.
    "home/schedule.py",
    "home/schedule_router.py",
    "home/observability/clickhouse.py",
    "home/observability/slo.py",
    "home/observability/rollup.py",
    "home/observability/stats.py",
    "home/observability/traces.py",
    # Grimoire: only the public read path ships. The daily ingest/extract jobs
    # import the scheduler (a forbidden public module), so they stay out of the
    # public binary; the read path (public/router_public/library/models/
    # visibility plus the extract/ingest helpers it calls) ships.
    "grimoire/jobs.py",
    # FaaS: only the public invocation path ships. The ingestion API (router.py)
    # is the authenticated private-tier author surface (no /api on public); the
    # write path (storage.py = S3, workload.py = K8s CR CRUD) is private-only; the
    # guest function code (functions/**) runs inside the
    # microVM, never in the monolith. register_public imports only
    # invoke_router_public -> invoke_router -> repository/embervm_client/models,
    # none of which pull the pruned files (main_public_imports_test enforces it).
    "faas/router.py",
    "faas/storage.py",
    "faas/workload.py",
    "faas/functions/**/*.py",
]

py_library(
    name = "monolith_public_backend",
    srcs = glob(
        [
            "app/**/*.py",
            "core/**/*.py",
            "framework/**/*.py",
            "artifact/**/*.py",
            "knowledge/**/*.py",
            "home/**/*.py",
            "shared/**/*.py",
            "ships/**/*.py",
            "hikes/**/*.py",
            "stars/**/*.py",
            "dr_jobs/**/*.py",
            "chat_public/**/*.py",
            "trips/**/*.py",
            "worldcup/**/*.py",
            "campsites/**/*.py",
            # Grimoire public read tier: the read modules (router_public, public,
            # library, models, visibility, extract, ingest) ship here so
            # main_public can mount register_public. The private grant router and
            # scheduler-bound jobs are never imported by the public closure
            # (main_public_imports_test enforces this).
            "grimoire/**/*.py",
            "grimoire_chat/**/*.py",
            # FaaS public invocation tier: only the invoke path ships (Task 13).
            # register_public mounts invoke_router_public, which pulls
            # invoke_router (shared relay) + repository + embervm_client + models.
            # The ingestion API (router.py) and the write path (storage.py,
            # workload.py) plus the guest function code (functions/**) are pruned
            # below; main_public_imports_test enforces they stay out of the closure.
            "faas/**/*.py",
            # Ember public tier (docs/plans/2026-07-18-ember-public-pages.md Task 1):
            # the demo-postgres core, public-safe by construction (never imports
            # demos/sandbox.client; EMBERVM_URL read directly from the environment).
            "ember_public/**/*.py",
            # Semgrep demo (ember_public.semgrep_router): only client.py + the
            # package __init__ ship; the rest is pruned below (private-only).
            "semgrep_scan/**/*.py",
        ],
        exclude = _PUBLIC_PRUNE_EXCLUDE,
    ),  # keep
    visibility = ["//:__subpackages__"],
    deps = [
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
        "@pip//lxml_html_clean",  # explicit: overrides.txt strips lxml[html-clean] extra
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
        "@pip//pyyaml",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
        "@pip//timelength",
        "@pip//trafilatura",
        "@pip//tzdata",
        "@pip//uvicorn",
        "@pip//websockets",
        "@pip//youtube_transcript_api",
    ],
)

py_venv_binary(
    name = "main_public",
    srcs = glob(
        [
            "app/**/*.py",
            "core/**/*.py",
            "framework/**/*.py",
            "artifact/**/*.py",
            "knowledge/**/*.py",
            "home/**/*.py",
            "shared/**/*.py",
            "ships/**/*.py",
            "hikes/**/*.py",
            "stars/**/*.py",
            "dr_jobs/**/*.py",
            "chat_public/**/*.py",
            "trips/**/*.py",
            "worldcup/**/*.py",
            "campsites/**/*.py",
            "grimoire/**/*.py",
            "grimoire_chat/**/*.py",
            # FaaS public invocation tier: only the invoke path ships (Task 13).
            # register_public mounts invoke_router_public, which pulls
            # invoke_router (shared relay) + repository + embervm_client + models.
            # The ingestion API (router.py) and the write path (storage.py,
            # workload.py) plus the guest function code (functions/**) are pruned
            # below; main_public_imports_test enforces they stay out of the closure.
            "faas/**/*.py",
            "ember_public/**/*.py",
            # Semgrep demo (ember_public.semgrep_router): only client.py + the
            # package __init__ ship; mcp/report/full_scan/router/perf stay
            # pruned below (private-tier surfaces).
            "semgrep_scan/**/*.py",
        ],
        exclude = _PUBLIC_PRUNE_EXCLUDE,
    ),  # keep
    imports = ["."],  # keep
    main = "app/main_public.py",
    visibility = ["//:__subpackages__"],
    deps = [":monolith_public_backend"],  # keep
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
    srcs = _BACKEND_SRCS,  # keep
    data = [
        # Same runtime data files as :main (any domain may be selected at
        # startup); only the static frontend bundle is omitted, because the
        # domain profile never serves it (framework: static_frontend=False).
        "campsites/catalog.json",
        "chat/directive_seed.md",
        "chat/orchestrator_bundle.md",
        "knowledge/repo_docs_manifest.ndjson",
        "worldcup/ratings/elo_2026.json",
    ] + glob(["demos/loadtest_corpus/**/*.sample"]),
    imports = ["."],  # keep
    main = "app/main_domain.py",
    visibility = ["//:__subpackages__"],
    deps = [":monolith_backend"],  # keep
)

monolith_domain_images(
    name = "domain_images",
    binary = ":main_domain",
    config_test_domain = "hikes",
    domains = MONOLITH_DOMAINS,
    main = "app/main_domain.py",
)

py_test(
    name = "auth_verifier_test",
    srcs = ["auth/verifier_test.py"],
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
    srcs = ["auth/plumbing_test.py"],
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
    srcs = ["shared/testing/markers_test.py"],
    imports = ["."],
    deps = [
        ":shared_testing",
        "@pip//pytest",
    ],
)

# Reads every domain source file from disk (via rglob) to enforce that domains
# only import each other through <domain>/api.py, so the scanned files must be
# materialised as data (the same pattern as bdd_completeness_test).
py_test(
    name = "import_boundaries_test",
    srcs = ["import_boundaries_test.py"],
    data = glob(
        include = [
            "ships/**/*.py",
            "stars/**/*.py",
            "artifact/**/*.py",
            "chat/**/*.py",
            "chat_public/**/*.py",
            "grimoire_chat/**/*.py",
            "knowledge/**/*.py",
            "hikes/**/*.py",
            "moving/**/*.py",
            "dr_jobs/**/*.py",
            "trips/**/*.py",
            "home/**/*.py",
            "scheduler/**/*.py",
            "agent/**/*.py",
            "agent_sessions/**/*.py",
            "auth/**/*.py",
            "goosecracker/**/*.py",
            "worldcup/**/*.py",
            "campsites/**/*.py",
        ],
        exclude = [
            "**/*_test.py",
            "**/test_*.py",
            "*/tests/**/*.py",
        ],
    ),
    imports = ["."],
    deps = ["@pip//pytest"],
)

py_test(
    name = "view_test",
    srcs = ["swarm/view_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "rows_test",
    srcs = ["swarm/rows_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "models_test",
    srcs = ["swarm/models_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "rationale_test",
    srcs = ["swarm/rationale_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        ":swarm_rationale",
        "@pip//pytest",
    ],
)

py_test(
    name = "deviations_test",
    srcs = ["swarm/deviations_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "tier_test",
    srcs = ["swarm/tier_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
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
    srcs = ["home/schedule_extra_test.py"],
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
    srcs = ["home/schedule_router_test.py"],
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
    srcs = ["home/dashboard_test.py"],
    imports = ["."],
    deps = [
        ":monolith_backend",
        ":monolith_public_backend",
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
    srcs = ["home/cluster_snapshot_test.py"],
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
    srcs = ["core/db_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "jobs_main_test",
    srcs = ["app/jobs_main_test.py"],
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
    name = "main_public_routes_test",
    srcs = ["app/main_public_routes_test.py"],
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
    srcs = ["app/main_public_imports_test.py"],
    imports = ["."],
    # Depend on the PRUNED public library, not :monolith_backend. This makes the
    # test a pre-deploy validator of the prune itself: if the public glob omits a
    # file that app.main_public actually needs, the child import fails
    # (returncode != 0) here in CI rather than only at pod startup. It still
    # asserts the forbidden private modules are absent from the closure.
    deps = [
        ":monolith_public_backend",
        "@pip//pytest",
    ],
)

py_test(
    name = "framework_core_test",
    srcs = ["framework/core_test.py"],
    imports = ["."],
    deps = [
        ":pkg_framework",
        "@pip//fastapi",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "main_domain_test",
    srcs = ["app/main_domain_test.py"],
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
    srcs = ["cluster/summarize_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cluster",
        "@pip//pytest",
    ],
)

py_test(
    name = "cluster_mcp_test",
    srcs = ["cluster/mcp_test.py"],
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
    srcs = ["semgrep_scan/mcp_test.py"],
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
    srcs = ["semgrep_scan/report_test.py"],
    data = [
        # report_test loads this captured cli_output fixture at runtime via
        # Path(__file__).parent/"testdata"; the "semgrep_scan/**/*.py" backend
        # glob does not capture the .json, so ship it into runfiles. 8 results
        # (7 findings + 1 ignored), derived from a real 1.168.0 Pro finding.
        "semgrep_scan/testdata/real_cli_output.json",
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
    srcs = ["semgrep_scan/router_test.py"],
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
    srcs = ["semgrep_scan/perf_store_test.py"],
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
    srcs = ["semgrep_scan/perf_harvest_test.py"],
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
    srcs = ["semgrep_scan/cohorts_test.py"],
    imports = ["."],
    # gazelle:exclude semgrep_scan means this py_test is hand-added, not generated.
    deps = [
        ":pkg_semgrep_scan",
        "@pip//pytest",
    ],
)

py_test(
    name = "semgrep_perf_compare_test",
    srcs = ["semgrep_scan/perf_compare_test.py"],
    imports = ["."],
    # gazelle:exclude semgrep_scan means this py_test is hand-added, not generated.
    deps = [
        ":pkg_semgrep_scan",
        "@pip//pytest",
    ],
)

py_test(
    name = "semgrep_perf_router_test",
    srcs = ["semgrep_scan/perf_router_test.py"],
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
    srcs = ["semgrep_scan/perf_webhook_test.py"],
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
    srcs = ["sandbox/mcp_test.py"],
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
    srcs = ["sandbox/client_test.py"],
    imports = ["."],
    deps = [
        ":pkg_sandbox",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

# Sessioned run_python (EmberVM R2): create-reuse-close, the 410 transparent
# re-create + session_reset flag, and the token-never-logged guard. Uses an
# in-memory SQLite for the sandbox.session table, so it needs sqlmodel.
py_test(
    name = "sandbox_session_test",
    srcs = ["sandbox/session_test.py"],
    imports = ["."],
    deps = [
        ":pkg_sandbox",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

# EmberVM R4 scratch-postgres DSN plumbing (D-R4.PR-11.1): asserts the DSN is
# injected into the submitted run_python code (guest-side psycopg-connect),
# repr-escaped, and reaches the POSTed payload; and that an unset DSN is a no-op.
py_test(
    name = "sandbox_scratch_postgres_test",
    srcs = ["sandbox/scratch_postgres_test.py"],
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
        ":pkg_cyclegroup",
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
        ":pkg_cyclegroup",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_models_reminder_test",
    srcs = ["chat/models_reminder_test.py"],
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
    srcs = ["chat/reminders_test.py"],
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
    srcs = ["chat/jobs_reminder_test.py"],
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
    srcs = ["chat/models_coverage_test.py"],
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
    srcs = ["shared/embedding_test.py"],
    imports = ["."],
    deps = [
        ":pkg_shared",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "shared_inference_test",
    srcs = ["shared/inference_test.py"],
    imports = ["."],
    deps = [
        ":pkg_shared",
        "@pip//pytest",
    ],
)

py_test(
    name = "shared_k8s_auth_test",
    srcs = ["shared/k8s_auth_test.py"],
    imports = ["."],
    deps = [
        ":pkg_shared",
        "@pip//pytest",
    ],
)

py_test(
    name = "shared_embedding_retry_test",
    srcs = ["shared/embedding_retry_test.py"],
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
    srcs = ["shared/embedding_batch_test.py"],
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
    srcs = ["chat/store_test.py"],
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
    srcs = ["chat/query_stats_test.py"],
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
    srcs = ["chat/store_fetch_window_test.py"],
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
    srcs = ["chat/digest_test.py"],
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
    srcs = ["chat/channel_data_test.py"],
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
    srcs = ["chat/observer_test.py"],
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
    srcs = ["chat/observer_job_test.py"],
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
    srcs = ["chat/outbox_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_whatsapp_outbox_test",
    srcs = ["chat/whatsapp_outbox_test.py"],
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
    srcs = ["chat/whatsapp_group_test.py"],
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
    srcs = ["chat/whatsapp_inbound_test.py"],
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
    srcs = ["chat/whatsapp_timeparse_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_whatsapp_intents_test",
    srcs = ["chat/whatsapp_intents_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_whatsapp_calendar_test",
    srcs = ["chat/whatsapp_calendar_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_whatsapp_capabilities_test",
    srcs = ["chat/whatsapp_capabilities_test.py"],
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
    srcs = ["chat/whatsapp_digest_test.py"],
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
    srcs = ["chat/orchestrator_bundle_test.py"],
    # The test reads both the hand-written base prompt and the committed
    # generated bundle directly from runfiles (Path(__file__).parent), so ship
    # them as data (mirrors chat_directives_test's directive_seed.md). The
    # generator itself is exercised via its pure functions only: the git
    # ls-files-driven path discovery has no .git to walk inside the bazel test
    # sandbox, same reason gen_repo_docs_manifest_test.py never calls
    # iter_doc_paths.
    data = [
        "chat/orchestrator_bundle.md",
        "chat/orchestrator_prompt.md",
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
    srcs = ["chat/acl_test.py"],
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
    srcs = ["chat/attention_log_test.py"],
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
    srcs = ["chat/directives_test.py"],
    # directives.get_active reads the committed seed from runfiles via
    # Path(__file__).parent / "directive_seed.md", so ship it as test data
    # (mirrors campsites_client_test's catalog.json).
    data = ["chat/directive_seed.md"],
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
    srcs = ["chat/ambient_analysis_test.py"],
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
    srcs = ["chat/autopilot_job_test.py"],
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
    srcs = ["chat/attention_test.py"],
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
    srcs = ["chat/reply_sanitize_test.py"],
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
    srcs = ["chat/web_search_test.py"],
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
    srcs = ["chat/bot_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//discord_py",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_bot_send_message_test",
    srcs = ["chat/bot_send_message_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_agent_test",
    srcs = ["chat/agent_test.py"],
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
    srcs = ["chat/store_coverage_test.py"],
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
    srcs = ["chat/bot_on_message_test.py"],
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
    srcs = ["chat/bot_streaming_test.py"],
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
    srcs = ["chat/web_search_coverage_test.py"],
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
    srcs = ["shared/embedding_coverage_test.py"],
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
    srcs = ["chat/agent_deps_test.py"],
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
    srcs = ["chat/agent_coverage_test.py"],
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
    name = "leadership_test",
    srcs = ["core/leadership_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_agent_extra_test",
    srcs = ["chat/agent_extra_test.py"],
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
    srcs = ["chat/bot_extra_test.py"],
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
    srcs = ["chat/web_search_extra_test.py"],
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
    srcs = ["chat/store_extra_test.py"],
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
        ":pkg_shared",
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
    srcs = ["chat/changelog_fetch_test.py"],
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
    srcs = ["chat/summarizer_test.py"],
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
    srcs = ["core/log_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        "@pip//pytest",
    ],
)

py_test(
    name = "log_config_test",
    srcs = ["core/log_config_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
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
    name = "store_integrity_test",
    srcs = ["chat/store_integrity_test.py"],
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
    srcs = ["chat/bot_self_message_test.py"],
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
    srcs = ["chat/bot_embed_test.py"],
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
    srcs = ["chat/web_search_headers_test.py"],
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
    srcs = ["chat/models_summary_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "chat_store_channel_summary_test",
    srcs = ["chat/store_channel_summary_test.py"],
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
    srcs = ["chat/store_summary_test.py"],
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
    srcs = ["chat/vision_errors_test.py"],
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
    srcs = ["chat/orchestrator_client_test.py"],
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
    srcs = ["chat/orchestrator_plan_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_orchestrator_test",
    srcs = ["chat/orchestrator_test.py"],
    # compile() reads the committed bundle from runfiles (Path(__file__).parent),
    # so ship it as data (mirrors chat_orchestrator_bundle_test).
    data = ["chat/orchestrator_bundle.md"],
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
    srcs = ["chat/summarizer_llm_caller_test.py"],
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
    srcs = ["chat/summarizer_agent_reply_test.py"],
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
    srcs = ["chat/bot_agent_prompt_echo_test.py"],
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
    srcs = ["chat/summarizer_channel_test.py"],
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
    srcs = ["chat/bot_exception_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_bot_error_handling_test",
    srcs = ["chat/bot_error_handling_test.py"],
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
    srcs = ["chat/agent_tools_test.py"],
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
    srcs = ["chat/store_attachments_test.py"],
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
    srcs = ["chat/agent_coerce_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_store_blob_test",
    srcs = ["chat/store_blob_test.py"],
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
    srcs = ["chat/store_flush_blob_test.py"],
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
    srcs = ["chat/store_save_messages_test.py"],
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
    srcs = ["chat/store_embed_text_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_agent_tool_execution_test",
    srcs = ["chat/agent_tool_execution_test.py"],
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
    srcs = ["chat/agent_channel_digest_tool_test.py"],
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
    srcs = ["chat/agent_reminder_tools_test.py"],
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
    srcs = ["chat/bot_attachments_test.py"],
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
    srcs = ["chat/bot_multimodal_test.py"],
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
    srcs = ["chat/startup_test.py"],
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
    srcs = ["chat/summarizer_startup_test.py"],
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
    srcs = ["chat/summarizer_prompts_test.py"],
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
    srcs = ["chat/vision_timeout_test.py"],
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
    srcs = ["chat/web_search_null_results_test.py"],
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
    srcs = ["chat/models_persistence_test.py"],
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
    srcs = ["chat/store_upsert_test.py"],
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
    srcs = ["chat/store_bulk_test.py"],
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
    srcs = ["chat/models_validation_test.py"],
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
    srcs = ["chat/backfill_test.py"],
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
    srcs = ["chat/vision_test.py"],
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
    srcs = ["chat/chat_new_coverage_test.py"],
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
    srcs = ["chat/web_search_missing_fields_test.py"],
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
    srcs = ["chat/store_non_integrity_error_test.py"],
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
    srcs = ["chat/summarizer_none_id_test.py"],
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
    srcs = ["shared/embedding_null_test.py"],
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
    srcs = ["chat/bot_session_failure_test.py"],
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
    srcs = ["chat/bot_thinking_test.py"],
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
    srcs = ["chat/bot_embeddable_content_test.py"],
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
    srcs = ["chat/agent_max_tokens_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pydantic_ai_slim",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_agent_signpost_test",
    srcs = ["chat/agent_signpost_test.py"],
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
    srcs = ["shared/embedding_retryable_test.py"],
    imports = ["."],
    deps = [
        ":pkg_shared",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_vision_retryable_test",
    srcs = ["chat/vision_retryable_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_store_null_data_test",
    srcs = ["chat/store_null_data_test.py"],
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
    srcs = ["chat/bot_auto_search_test.py"],
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
    srcs = ["chat/bot_response_storage_test.py"],
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
    srcs = ["shared/embedding_connect_timeout_test.py"],
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
    srcs = ["chat/agent_web_search_tool_test.py"],
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
    srcs = ["chat/bot_summary_injection_test.py"],
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
    srcs = ["chat/bot_identity_note_test.py"],
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
    srcs = ["chat/bot_subject_note_test.py"],
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
    srcs = ["chat/bot_attachment_download_test.py"],
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
    srcs = ["chat/router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "agent_sessions_router_test",
    srcs = ["agent_sessions/router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        ":pkg_core",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "swarm_config_test",
    srcs = ["swarm/config_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_policy_test",
    srcs = ["swarm/policy_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_classifier_test",
    srcs = ["swarm/classifier_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_router_test",
    srcs = ["swarm/router_test.py"],
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
    srcs = ["swarm/compare_router_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_unified_diff_test",
    srcs = ["swarm/unified_diff_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_walkthrough_composer_test",
    srcs = ["swarm/walkthrough_composer_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_workflows_test",
    srcs = ["swarm/workflows_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//pytest",
    ],
)

py_test(
    name = "swarm_steps_test",
    srcs = ["swarm/steps_test.py"],
    imports = ["."],
    deps = [
        ":pkg_swarm",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "agent_sessions_store_test",
    srcs = ["agent_sessions/store_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_api_test",
    srcs = ["agent_sessions/api_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_progress_ingest_test",
    srcs = ["agent_sessions/progress_ingest_test.py"],
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
    name = "chat_router_backfill_exception_test",
    srcs = ["chat/router_backfill_exception_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "chat_backfill_flush_test",
    srcs = ["chat/backfill_flush_test.py"],
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
    srcs = ["chat/store_find_user_test.py"],
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
    srcs = ["chat/store_search_model_validate_test.py"],
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
    srcs = ["chat/bot_thinking_view_callback_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
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
        ":pkg_cyclegroup",
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
        ":pkg_cyclegroup",
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
    name = "scheduler_test",
    srcs = ["scheduler/scheduler_test.py"],
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
    srcs = ["scheduler/argo_handled_test.py"],
    imports = ["."],
    deps = [
        ":pkg_scheduler",
        "@pip//pytest",
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
        ":pkg_cyclegroup",
        "@pip//icalendar",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//tzdata",
    ],
)

py_test(
    name = "shared_forecast_freshness_test",
    srcs = ["shared/forecast_freshness_test.py"],
    imports = ["."],
    deps = [
        ":pkg_shared",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_chunker_test",
    srcs = ["knowledge/chunker_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_chunker_extra_test",
    srcs = ["knowledge/chunker_extra_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_chunker_split_edge_cases_test",
    srcs = ["knowledge/chunker_split_edge_cases_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_frontmatter_test",
    srcs = ["knowledge/frontmatter_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

py_test(
    name = "knowledge_layout_test",
    srcs = ["knowledge/layout_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//networkx",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_links_test",
    srcs = ["knowledge/links_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_links_extra_test",
    srcs = ["knowledge/links_extra_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_links_coverage_test",
    srcs = ["knowledge/links_coverage_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_store_test",
    srcs = ["knowledge/store_test.py"],
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
    name = "knowledge_indexing_test",
    srcs = ["knowledge/indexing_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "knowledge_wikilinks_test",
    srcs = ["knowledge/wikilinks_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_visibility_test",
    srcs = ["knowledge/visibility_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_profile_test",
    srcs = ["knowledge/profile_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_tools_add_visibility_field_test",
    srcs = ["knowledge/tools/add_visibility_field_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_tools_classify_visibility_backfill_test",
    srcs = ["knowledge/tools/classify_visibility_backfill_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_gen_repo_docs_manifest_test",
    srcs = ["knowledge/gen_repo_docs_manifest_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_gen_docs_manifest_test",
    srcs = ["knowledge/gen_docs_manifest_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_repo_docs_test",
    srcs = ["knowledge/repo_docs_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_gardener_test",
    srcs = ["knowledge/gardener_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_models_test",
    srcs = ["knowledge/models_test.py"],
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
    srcs = ["grimoire/models_test.py"],
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
    srcs = ["grimoire/visibility_test.py"],
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
    srcs = ["grimoire/router_test.py"],
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
    srcs = ["grimoire/entities_test.py"],
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
    srcs = ["grimoire/search_test.py"],
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
    srcs = ["grimoire/library_test.py"],
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
    srcs = ["grimoire/public_test.py"],
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
    srcs = ["grimoire/explore_test.py"],
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
    srcs = ["grimoire/router_public_test.py"],
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
    srcs = ["grimoire/ingest_test.py"],
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
    srcs = ["grimoire/extract_test.py"],
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
    srcs = ["grimoire/marker_test.py"],
    imports = ["."],
    deps = [
        ":pkg_grimoire",
        "@pip//pytest",
    ],
)

py_test(
    name = "grimoire_jobs_test",
    srcs = ["grimoire/jobs_test.py"],
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
    srcs = ["grimoire/hierarchy_backfill_test.py"],
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
    srcs = ["ships/models_test.py"],
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
        "moving/tests/__init__.py",
        "moving/tests/conftest.py",
        "moving/tests/models_test.py",
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
        "moving/tests/__init__.py",
        "moving/tests/collisions_test.py",
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
        "moving/tests/__init__.py",
        "moving/tests/conftest.py",
        "moving/tests/viewer_test.py",
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
        "moving/tests/__init__.py",
        "moving/tests/conftest.py",
        "moving/tests/router_test.py",
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
    name = "hikes_models_test",
    srcs = ["hikes/models_test.py"],
    imports = ["."],
    deps = [
        ":pkg_hikes",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "faas_models_test",
    srcs = ["faas/models_test.py"],
    imports = ["."],
    deps = [
        ":pkg_faas",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "faas_storage_test",
    srcs = ["faas/storage_test.py"],
    imports = ["."],
    deps = [
        ":pkg_faas",
        "@pip//boto3",
        "@pip//pytest",
    ],
)

py_test(
    name = "faas_runtime_test",
    srcs = ["faas/runtime_test.py"],
    imports = ["."],
    deps = [
        ":pkg_faas",
        "@pip//pytest",
    ],
)

py_test(
    name = "faas_embervm_client_test",
    srcs = ["faas/embervm_client_test.py"],
    imports = ["."],
    deps = [
        ":pkg_faas",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "faas_workload_test",
    srcs = ["faas/workload_test.py"],
    imports = ["."],
    deps = [
        ":pkg_faas",
        "@pip//kubernetes_asyncio",
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

py_test(
    name = "faas_router_test",
    srcs = ["faas/router_test.py"],
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
    srcs = ["faas/invoke_router_test.py"],
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
    srcs = ["faas/invoke_router_public_test.py"],
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
    srcs = ["stars/models_test.py"],
    imports = ["."],
    deps = [
        ":pkg_stars",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "stars_scoring_test",
    srcs = ["stars/scoring_test.py"],
    imports = ["."],
    deps = [
        ":pkg_stars",
        "@pip//pydantic",
        "@pip//pytest",
    ],
)

py_test(
    name = "stars_forecast_test",
    srcs = ["stars/forecast_test.py"],
    imports = ["."],
    deps = [
        ":pkg_stars",
        "@pip//pytest",
    ],
)

py_test(
    name = "stars_jobs_test",
    srcs = ["stars/jobs_test.py"],
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
    srcs = ["stars/grid_test.py"],
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
    srcs = ["stars/climatology_test.py"],
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
    srcs = ["stars/router_test.py"],
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
    srcs = ["stars/health_test.py"],
    imports = ["."],
    deps = [
        ":pkg_stars",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "hikes_walkhighlands_test",
    srcs = ["hikes/walkhighlands_test.py"],
    imports = ["."],
    deps = [
        ":pkg_hikes",
        "@pip//pytest",
    ],
)

py_test(
    name = "hikes_forecast_test",
    srcs = ["hikes/forecast_test.py"],
    imports = ["."],
    deps = [
        ":pkg_hikes",
        "@pip//pytest",
    ],
)

py_test(
    name = "hikes_router_test",
    srcs = ["hikes/router_test.py"],
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
    srcs = ["chat_public/router_test.py"],
    imports = ["."],
    deps = [
        ":monolith_public_backend",
        "@pip//fastapi",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

# Artifact service: TestClient unit tests for the publish and read endpoints
# (S3 faked) and the S3 helper (boto3 mocked).
py_test(
    name = "artifact_router_test",
    srcs = ["artifact/router_test.py"],
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
    srcs = ["artifact/s3_test.py"],
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
    srcs = ["chat_public/snapshots_test.py"],
    imports = ["."],
    deps = [
        ":monolith_public_backend",
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
    srcs = ["chat_public/phase2_test.py"],
    imports = ["."],
    deps = [
        ":monolith_public_backend",
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
    srcs = ["chat_public/phase3_test.py"],
    imports = ["."],
    deps = [
        ":monolith_public_backend",
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
    srcs = ["chat_public/phase4_test.py"],
    imports = ["."],
    deps = [
        ":monolith_public_backend",
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
    srcs = ["chat_public/phase5_test.py"],
    imports = ["."],
    deps = [
        ":monolith_public_backend",
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
    srcs = ["grimoire_chat/retrieval_test.py"],
    imports = ["."],
    deps = [
        ":monolith_public_backend",
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
    srcs = ["grimoire_chat/router_test.py"],
    imports = ["."],
    deps = [
        ":monolith_public_backend",
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
    srcs = ["hikes/jobs_test.py"],
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
    srcs = ["hikes/__init___test.py"],
    imports = ["."],
    deps = [
        ":pkg_hikes",
        "@pip//pytest",
    ],
)

py_test(
    name = "dr_jobs_models_test",
    srcs = ["dr_jobs/models_test.py"],
    imports = ["."],
    deps = [
        ":pkg_dr_jobs",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "dr_jobs_scraper_test",
    srcs = ["dr_jobs/scraper_test.py"],
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
    srcs = ["dr_jobs/router_test.py"],
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
    srcs = ["dr_jobs/jobs_test.py"],
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
    srcs = ["worldcup/ratings_test.py"],
    # ratings.load_elo() reads the committed Elo snapshot at runtime.
    data = ["worldcup/ratings/elo_2026.json"],
    imports = ["."],
    deps = [
        ":pkg_worldcup",
        "@pip//pytest",
    ],
)

py_test(
    name = "worldcup_outcome_test",
    srcs = ["worldcup/outcome_test.py"],
    imports = ["."],
    deps = [
        ":pkg_worldcup",
        "@pip//pytest",
    ],
)

py_test(
    name = "worldcup_sim_test",
    srcs = ["worldcup/sim_test.py"],
    data = ["worldcup/ratings/elo_2026.json"],
    imports = ["."],
    deps = [
        ":pkg_worldcup",
        "@pip//pytest",
    ],
)

py_test(
    name = "worldcup_strength_test",
    srcs = ["worldcup/strength_test.py"],
    imports = ["."],
    deps = [
        ":pkg_worldcup",
        "@pip//pytest",
    ],
)

py_test(
    name = "worldcup_client_test",
    srcs = ["worldcup/client_test.py"],
    imports = ["."],
    deps = [
        ":pkg_worldcup",
        "@pip//httpx",
        "@pip//pytest",
    ],
)

py_test(
    name = "worldcup_jobs_test",
    srcs = ["worldcup/jobs_test.py"],
    data = ["worldcup/ratings/elo_2026.json"],
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
    srcs = ["worldcup/router_test.py"],
    data = ["worldcup/ratings/elo_2026.json"],
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
    srcs = ["campsites/client_test.py"],
    # The load_catalog / _read_catalog_json tests read the committed static
    # catalog from runfiles, so ship it as test data.
    data = ["campsites/catalog.json"],
    imports = ["."],
    deps = [
        ":pkg_campsites",
        "@pip//pytest",
    ],
)

py_test(
    name = "campsites_weather_test",
    srcs = ["campsites/weather_test.py"],
    imports = ["."],
    deps = [
        ":pkg_campsites",
        "@pip//pytest",
    ],
)

py_test(
    name = "campsites_jobs_test",
    srcs = ["campsites/jobs_test.py"],
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
    srcs = ["campsites/router_test.py"],
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
    srcs = ["stars/__init___test.py"],
    imports = ["."],
    deps = [
        ":pkg_stars",
        "@pip//pytest",
    ],
)

py_test(
    name = "hikes_generate_seed_test",
    srcs = [
        "hikes/tools/generate_seed.py",
        "hikes/tools/generate_seed_test.py",
    ],
    imports = ["hikes/tools"],
    deps = ["@pip//pytest"],
)

py_test(
    name = "stars_generate_grid_test",
    srcs = [
        "stars/grid_gen/generate_grid.py",
        "stars/grid_gen/generate_grid_test.py",
    ],
    imports = ["stars/grid_gen"],
    deps = ["@pip//pytest"],
)

py_test(
    name = "stars_generate_grid_v2_test",
    srcs = [
        "stars/grid_gen/generate_grid_v2.py",
        "stars/grid_gen/generate_grid_v2_test.py",
    ],
    imports = ["stars/grid_gen"],
    deps = ["@pip//pytest"],
)

py_test(
    name = "ships_heat_test",
    srcs = ["ships/heat_test.py"],
    imports = ["."],
    deps = [
        ":pkg_ships",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "ships_ais_test",
    srcs = ["ships/ais_test.py"],
    imports = ["."],
    deps = [
        ":pkg_ships",
        "@pip//pytest",
    ],
)

py_test(
    name = "ships_store_test",
    srcs = ["ships/store_test.py"],
    imports = ["."],
    deps = [
        ":pkg_ships",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "ships_ingest_test",
    srcs = ["ships/ingest_test.py"],
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
    srcs = ["ships/router_test.py"],
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
    srcs = ["ships/retention_test.py"],
    imports = ["."],
    deps = [
        ":pkg_ships",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_gap_model_test",
    srcs = ["knowledge/gap_model_test.py"],
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
    name = "knowledge_gap_discover_fileless_test",
    srcs = ["knowledge/gap_discover_fileless_test.py"],
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
    name = "knowledge_gap_commit_hook_test",
    srcs = ["knowledge/gap_commit_hook_test.py"],
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
    srcs = ["knowledge/gap_set_class_test.py"],
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
    srcs = ["knowledge/gap_answer_fileless_test.py"],
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
    name = "knowledge_deleted_at_filter_test",
    srcs = ["knowledge/deleted_at_filter_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_gap_logic_test",
    srcs = ["knowledge/gap_logic_test.py"],
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
    name = "knowledge_gap_slug_resolution_test",
    srcs = ["knowledge/gap_slug_resolution_test.py"],
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
    name = "knowledge_store_gap_queries_test",
    srcs = ["knowledge/store_gap_queries_test.py"],
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
    srcs = ["knowledge/service_test.py"],
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
        ":pkg_cyclegroup",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "knowledge_store_extra_test",
    srcs = ["knowledge/store_extra_test.py"],
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
    srcs = ["knowledge/frontmatter_extra_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

py_test(
    name = "knowledge_frontmatter_json_safe_test",
    srcs = ["knowledge/frontmatter_json_safe_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
        "@pip//pyyaml",
    ],
)

py_test(
    name = "chat_store_reclaim_race_test",
    srcs = ["chat/store_reclaim_race_test.py"],
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
    srcs = ["home/schedule_naive_dtend_test.py"],
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
    srcs = ["knowledge/store_zero_chunk_test.py"],
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
    srcs = ["knowledge/store_optional_metadata_test.py"],
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
    srcs = ["knowledge/raw_paths_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_migrate_raw_bucketing_test",
    srcs = ["knowledge/migrate_raw_bucketing_test.py"],
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
    srcs = ["knowledge/knowledge_fixes_coverage_test.py"],
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
    srcs = ["knowledge/router_test.py"],
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
    name = "knowledge_notes_crud_test",
    srcs = ["knowledge/notes_crud_test.py"],
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
    srcs = ["knowledge/mcp_test.py"],
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
    name = "knowledge_gap_api_test",
    srcs = ["knowledge/gap_api_test.py"],
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
    srcs = ["knowledge/gap_review_endpoints_test.py"],
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
    srcs = ["knowledge/note_review_endpoints_test.py"],
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
    srcs = ["knowledge/notes_test.py"],
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
    name = "knowledge_mcp_gap_test",
    srcs = ["knowledge/mcp_gap_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
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
        ":pkg_core",
        ":pkg_framework",
        ":pkg_knowledge",
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
        ":pkg_knowledge",
        "@pip//pgvector",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "knowledge_raw_store_test",
    srcs = ["knowledge/raw_store_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//boto3",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_store_note_links_test",
    srcs = ["knowledge/store_note_links_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
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
        ":pkg_knowledge",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "knowledge_ingest_queue_claim_test",
    srcs = ["knowledge/ingest_queue_claim_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
    ],
)

py_test(
    name = "knowledge_ingest_queue_webpage_title_test",
    srcs = ["knowledge/ingest_queue_webpage_title_test.py"],
    imports = ["."],
    deps = [
        ":pkg_knowledge",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "chat_web_search_type_error_test",
    srcs = ["chat/web_search_type_error_test.py"],
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
    srcs = ["chat/sse_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//anyio",
        "@pip//pytest",
    ],
)

py_test(
    name = "explorer_test",
    srcs = ["chat/explorer_test.py"],
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
    srcs = ["chat/explore_router_test.py"],
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
    srcs = ["chat/cluster_agent_test.py"],
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
    srcs = ["knowledge/ingest_queue_router_test.py"],
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
    srcs = ["knowledge/dead_letter_test.py"],
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
    srcs = ["knowledge/tasks_store_test.py"],
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
    srcs = ["knowledge/tasks_rollup_test.py"],
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
    name = "home_observability_clickhouse_test",
    srcs = ["home/observability/clickhouse_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "home_observability_slo_test",
    srcs = ["home/observability/slo_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "home_observability_router_test",
    srcs = ["home/observability/router_test.py"],
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
        # TODO: fix dr_jobs, ships, hikes, stars logger names to use "monolith."
        # prefix. semgrep-core does not honour inline # nosemgrep, so excluded
        # here until all callers are updated.
        "logger-name-missing-monolith-prefix",  # keep
    ],
    rules = ["//bazel/semgrep/rules:python_rules"],
    target = ":main",
)

py_test(
    name = "home_observability_stats_test",
    srcs = ["home/observability/stats_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "home_observability_traces_test",
    srcs = ["home/observability/traces_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "demos_firecracker_api_test",
    srcs = ["demos/firecracker_api_test.py"],
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
    srcs = ["ember_public/router_test.py"],
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
    srcs = ["ember_public/bazel_core_test.py"],
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
    srcs = ["ember_public/health_test.py"],
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
    srcs = ["ember_public/synthetic_health_test.py"],
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
    srcs = ["ember_public/synthetic_probe_test.py"],
    imports = ["."],
    deps = [
        # probe_qwen imports agent_sessions.api lazily, inside the function, so
        # the public binary never pulls it in at module load (main_public_imports
        # _test stays green and the public-tier boundary is unaffected). The
        # probe endpoint is served by the PRIVATE monolith, which is where the
        # trigger POSTs, so the lazy import resolves at runtime. The test still
        # needs the real module on the path because it monkeypatches
        # agent_sessions.api.run_synthetic_session, and monkeypatch imports the
        # target module to resolve the attribute.
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
    srcs = ["ember_public/synthetic_router_test.py"],
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
    srcs = ["demos/loadtest_corpus_test.py"],
    data = glob(["demos/loadtest_corpus/**/*.sample"]),
    imports = ["."],
    deps = [
        ":pkg_demos",
        "@pip//pytest",
    ],
)

py_test(
    name = "demos_loadtest_test",
    srcs = ["demos/loadtest_test.py"],
    # run_load_test defaults its corpus to load_corpus(workload), which reads
    # the committed .sample files at runtime.
    data = glob(["demos/loadtest_corpus/**/*.sample"]),
    imports = ["."],
    deps = [
        ":pkg_demos",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "home_observability_rollup_test",
    srcs = ["home/observability/rollup_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "cluster_kubernetes_test",
    srcs = ["cluster/kubernetes_test.py"],
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
    srcs = ["core/platform_probe_test.py"],
    imports = ["."],
    deps = [
        ":pkg_core",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "cluster_cd_health_test",
    srcs = ["cluster/cd_health_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cluster",
        "@pip//pytest",
        "@pip//pytest_asyncio",
    ],
)

py_test(
    name = "scheduler_views_test",
    srcs = ["scheduler/views_test.py"],
    imports = ["."],
    deps = [
        ":pkg_scheduler",
        "@pip//pydantic",
        "@pip//pytest",
    ],
)

py_test(
    name = "scheduler_service_test",
    srcs = ["scheduler/service_test.py"],
    imports = ["."],
    deps = [
        ":pkg_scheduler",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "scheduler_router_test",
    srcs = ["scheduler/router_test.py"],
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
        "home/tests/__init__.py",
        "home/tests/bdd_api_test.py",
        "home/tests/conftest.py",
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

py_test(
    name = "bdd_completeness_test",
    size = "small",
    srcs = [
        "app/__init__.py",
        "app/bdd_completeness_test.py",
    ],
    data = glob(["*/tests/*_test.py"]),
    env = {"PYTEST_ADDOPTS": "--import-mode=importlib --ignore=projects/monolith/home/tests --ignore=projects/monolith/chat/tests --ignore=projects/monolith/knowledge/tests --ignore=projects/monolith/scheduler/tests --ignore=projects/monolith/agent/tests --ignore=projects/monolith/goosecracker/tests --ignore=projects/monolith/moving/tests"},
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
        ":pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "agent_notify_test",
    srcs = ["agent/notify_test.py"],
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
    name = "goosecracker_repo_catalog_test",
    srcs = ["goosecracker/tests/repo_catalog_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "goosecracker_recipe_catalog_test",
    srcs = ["goosecracker/tests/recipe_catalog_test.py"],
    data = glob(["goosecracker/recipes/*.yaml"]),
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
        "@pip//pytest",
    ],
)

py_test(
    name = "agent_checks_firing_alerts_test",
    srcs = ["agent/checks_firing_alerts_test.py"],
    imports = ["."],
    deps = [
        ":pkg_cyclegroup",
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
        ":pkg_core",
        ":pkg_cyclegroup",
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

py_test(
    name = "agent_sessions_store_voice_test",
    srcs = ["agent_sessions/store_voice_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_titles_test",
    srcs = ["agent_sessions/titles_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        "@pip//httpx",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_mcp_test",
    srcs = ["agent_sessions/mcp_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        "@pip//fastmcp",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "agent_sessions_transport_test",
    srcs = ["agent_sessions/transport_test.py"],
    imports = ["."],
    deps = [
        ":pkg_agent_sessions",
        "@pip//httpx",
        "@pip//pytest",
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
py_library(
    name = "trips_backfill_lib",
    srcs = glob(
        ["trips/**/*.py"],
        exclude = ["trips/**/*_test.py"],
    ),
    imports = ["."],
    visibility = ["//:__subpackages__"],
    deps = [
        "@pip//boto3",
        "@pip//botocore",
        "@pip//defusedxml",
        "@pip//httpx",
        "@pip//pillow",
        "@pip//pyyaml",
        "@pip//sqlalchemy",
        "@pip//sqlmodel",
        "@pip//typer",
        "@pip//tzdata",
    ],
)

py_venv_binary(
    name = "trips_backfill",
    srcs = glob(
        ["trips/**/*.py"],
        exclude = ["trips/**/*_test.py"],
    ),
    main = "trips/backfill/main.py",
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
    srcs = ["trips/backfill/main_test.py"],
    imports = ["."],
    deps = [
        ":trips_backfill_lib",
        "@pip//pytest",
    ],
)

py_test(
    name = "trips_models_test",
    srcs = ["trips/models_test.py"],
    imports = ["."],
    deps = [
        ":pkg_trips",
        "@pip//pytest",
        "@pip//sqlmodel",
    ],
)

py_test(
    name = "trips_transform_test",
    srcs = ["trips/transform_test.py"],
    imports = ["."],
    deps = [
        ":pkg_trips",
        "@pip//pytest",
    ],
)

py_test(
    name = "trips_exif_test",
    srcs = ["trips/exif_test.py"],
    imports = ["."],
    deps = [
        ":pkg_trips",
        "@pip//pytest",
    ],
)

py_test(
    name = "trips_ingest_test",
    srcs = ["trips/ingest_test.py"],
    # Tiny committed JPEG fixtures (geotagged + no-GPS) the test reads through
    # the real exif.extract_exif path. Excluded from the server libs above so
    # the bytes never ship in the production image.
    data = glob(["trips/testdata/**"]),
    imports = ["."],
    deps = [
        ":pkg_trips",
        "@pip//pytest",
    ],
)

py_test(
    name = "trips_ingest_router_test",
    srcs = ["trips/ingest_router_test.py"],
    # Same committed JPEG fixtures posted through the TestClient.
    data = glob(["trips/testdata/**"]),
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
    srcs = ["trips/read_router_test.py"],
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
        # TODO: fix dr_jobs, ships, hikes, stars, app/main_public.py logger names
        # to use "monolith." prefix. semgrep-core does not honour inline
        # # nosemgrep, so excluded here until all callers are updated.
        "logger-name-missing-monolith-prefix",  # keep
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
    srcs = ["chat_public/summarizer_test.py"],
    imports = ["."],
    deps = [
        ":monolith_public_backend",
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
    srcs = ["chat_public/sessions_test.py"],
    imports = ["."],
    deps = [
        ":monolith_public_backend",
        "@pip//pytest",
        "@pip//pytest_asyncio",
        "@pip//sqlmodel",
    ],
)

# Hand-registered (knowledge is gazelle-excluded): pure HTTP-cache helpers
# (_as_utc, _graph_etag, _GRAPH_CACHE_CONTROL).
py_test(
    name = "knowledge_http_cache_test",
    srcs = ["knowledge/http_cache_test.py"],
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
    srcs = ["knowledge/public_models_test.py"],
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
    srcs = ["knowledge/public_router_test.py"],
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

# Hand-registered (trips is gazelle-excluded): SeaweedFS S3 image upload --
# missing endpoint, scheme guard, head-hit skip, auto-create bucket.
py_test(
    name = "trips_s3_test",
    srcs = ["trips/s3_test.py"],
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
        "stars/grid_gen/backfill_cerra.py",
        "stars/grid_gen/backfill_cerra_test.py",
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
        "stars/grid_gen/backfill_climatology.py",
        "stars/grid_gen/backfill_climatology_test.py",
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
        "logger-name-missing-monolith-prefix",  # keep
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
