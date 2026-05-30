# IMG-WORKER — projects/lakehouse/image (Temporal worker apko image)

**Unit:** IMG-WORKER (Wavefront 3)
**ADR:** [agents/015 — Temporal Orchestration Substrate](../../decisions/agents/015-temporal-orchestration-substrate.md) §"Worker pools, not the orchestrator"
**Branch:** `feat/lakehouse-img-worker`
**Classification:** purely-additive — one new dir `projects/lakehouse/image/` plus a
one-line insert into the auto-generated `bazel/images/BUILD` push list (exactly
what `//bazel/images:generate-push-all` would emit; pre-applied so the FIRST CI
run pushes the image and the format check sees no diff).

## What shipped

`projects/lakehouse/image/BUILD` — a single `py3_image` target, modeled
field-for-field on the monolith `image` target:

- `name = "image"`, `binary = "//projects/lakehouse/orchestrator/workflows:worker_main"`
  (the W3-PREP entrypoint, `python -m projects.lakehouse.orchestrator.workflows.run`,
  reads `TASK_QUEUE`).
- `multiarch_tars = ["@claude_code//:tar"]` — bundles the Claude Code CLI (same as
  the monolith backend image) so the gap-drain `run_research_session` activity can
  invoke the existing harness. Dual-arch via the macro's `_amd64`/`_arm64` split.
- `repository = "ghcr.io/jomcgi/homelab/projects/lakehouse/worker"`.
- `env`:
  - `PYTHONPATH = "/projects/lakehouse/orchestrator/workflows/worker_main.runfiles/_main"`
    — the macro's auto-computed workspace root. `run.py` uses absolute
    `from projects.lakehouse.orchestrator...` imports that resolve from that root,
    so (unlike the monolith, which adds its package dir for its `imports = ["."]`
    layout) only the workspace root is needed. Set explicitly for clarity.
  - `SSL_CERT_FILE = ".../worker_main.runfiles/_main/projects/lakehouse/orchestrator/workflows/.worker_main/lib/python3.13/site-packages/certifi/cacert.pem"`
    — certifi bundle (certifi==2026.2.25 is in `bazel/requirements/all.txt`,
    transitively pulled by temporalio/pyiceberg) for outbound HTTPS. Path follows
    the monolith pattern: `{runfiles}/_main/{binary.package}/.{binary.name}/lib/python3.13/site-packages/certifi/cacert.pem`.
- Non-root uid 65532 / dual-arch handled by the `py3_image` macro + `@python_base`.

## Design decisions

- **`main` intentionally omitted.** The macro only emits the supplementary
  `_srcs` tar when `main` is set, and that tar references
  `//projects/lakehouse/orchestrator/workflows:run.py` as a source-file label —
  which is **package-private** (the hand-written `workflows/BUILD` has no
  `exports_files`/`default_visibility`), so setting `main` cross-package would
  fail with a visibility error. `worker_main` already carries `run.py` in its
  runfiles (its glob `srcs` include it), so the tar is unnecessary. This mirrors
  the only other cross-package `py3_image` in the repo
  (`//projects/agent_platform/orchestrator/mcp:image`), which also omits `main`.

## One artifact, many Deployments

This is ONE image. Per-queue Deployments (Wavefront 4) set `TASK_QUEUE`
(gap-drain / iceberg-builder / housekeeping); there is no separate orchestrator
pod. KEDA scales each pool to queue depth.

## Validation

No local `bazel build` (Mac has no `workflows`-pool runner). Validated by:

- Manually replaying the `generate-push-all.sh` awk against the new BUILD →
  confirms it emits `//projects/lakehouse/image:image.push` (matches the
  pre-inserted line in `bazel/images/BUILD`).
- Field-by-field comparison against the monolith `image` target and the macro
  signature in `//bazel/tools/oci:py3_image.bzl`.
- CI "Push images" (`push_all`) + the macro's `image_config_test` are the real
  validation.
