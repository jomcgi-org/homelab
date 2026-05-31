# HAND-WRITTEN (py3_image is a custom macro; gazelle does not manage it).
# Modeled field-for-field on the monolith `image` target (projects/monolith/BUILD).
#
# ONE worker image for the event-sourced lakehouse. The image wraps the shared
# Temporal worker entrypoint (//projects/lakehouse/orchestrator/workflows:worker_main,
# `python -m projects.lakehouse.orchestrator.workflows.run`). Per ADR 015
# §"Worker pools, not the orchestrator", there is no single orchestrator pod:
# many Deployments share THIS image, differentiated only by the TASK_QUEUE env
# var (gap-drain / iceberg-builder / housekeeping). The per-queue Deployments
# are Wavefront 4; this unit (IMG-WORKER) ships only the image artifact.
load("//bazel/tools/oci:py3_image.bzl", "py3_image")

py3_image(
    name = "image",
    binary = "//projects/lakehouse/orchestrator/workflows:worker_main",
    env = {
        # The macro already sets PYTHONPATH to the workspace root
        # (worker_main.runfiles/_main). worker_main's run.py uses absolute
        # `from projects.lakehouse.orchestrator...` imports that resolve from
        # that root, so unlike the monolith (which adds its package dir for its
        # `imports = ["."]` layout) we only need the workspace root. Set it
        # explicitly so the runtime import path is obvious and self-documenting.
        "PYTHONPATH": "/projects/lakehouse/orchestrator/workflows/worker_main.runfiles/_main",
        # The minimal base image has no CA certificates; point OpenSSL at
        # certifi's bundle so outbound HTTPS works (Temporal Cloud/NATS TLS,
        # the Claude Code harness invoked by run_research_session, etc.).
        # The venv dir is `.{binary_name}` inside the binary's package dir.
        "SSL_CERT_FILE": "/projects/lakehouse/orchestrator/workflows/worker_main.runfiles/_main/projects/lakehouse/orchestrator/workflows/.worker_main/lib/python3.13/site-packages/certifi/cacert.pem",
    },
    # `main` is intentionally omitted (cross-package). The macro only emits the
    # supplementary `_srcs` tar — and only needs to — when `main` is set; that
    # tar would reference //projects/lakehouse/orchestrator/workflows:run.py as a
    # source-file label, which is package-private. worker_main already carries
    # run.py in its runfiles (its glob `srcs` include it), so the tar is
    # unnecessary. This mirrors the only other cross-package py3_image in the
    # repo (//projects/agent_platform/orchestrator/mcp:image), which likewise
    # omits `main`.
    # Bundle the Claude Code CLI (same as the monolith backend image) so the
    # gap-drain `run_research_session` activity can invoke the existing harness,
    # plus the signed DuckDB extensions (httpfs/iceberg/vss/avro/parquet) so the
    # iceberg-builder + read paths LOAD them from /opt/duckdb_ext/ rather than
    # downloading at runtime — the hardened image has no CA bundle DuckDB's
    # extension installer can use. See MODULE.bazel and duckdb_query.load_extensions.
    multiarch_tars = [
        "@claude_code//:tar",
        "@duckdb_ext_avro//:tar",
        "@duckdb_ext_httpfs//:tar",
        "@duckdb_ext_iceberg//:tar",
        "@duckdb_ext_parquet//:tar",
        "@duckdb_ext_vss//:tar",
    ],
    repository = "ghcr.io/jomcgi/homelab/projects/lakehouse/worker",
)
