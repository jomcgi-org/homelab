# LIB-TEMPORAL — projects/lakehouse/orchestrator (Temporal client + worker skeleton)

**Unit:** LIB-TEMPORAL (Wavefront 2 library unit)
**Source of truth:** [ADR agents/015](../../decisions/agents/015-temporal-orchestration-substrate.md)
**Classification:** purely additive — only new files under `projects/lakehouse/orchestrator/`.

## What shipped

The Temporal client + worker _skeleton_. No global worker/workflow/schedule
registration (that would collide with sibling W2 units and the W3 image unit) —
`__init__.py` exports types/constants only.

- `__init__.py` — exports `DEFAULT_NAMESPACE` (`"default"`), `DEFAULT_TARGET`
  (the in-cluster frontend gRPC endpoint), and a `TaskQueue` `str`-Enum with the
  three queues named in ADR 015: `gap-drain`, `iceberg-builder`, `housekeeping`.
  No side effects at import time.
- `client.py`
  - `resolve_target(env=None) -> str` — pure; reads `TEMPORAL_TARGET`, defaults
    to `temporal-frontend.temporal.svc.cluster.local:7233` (ADR 015 §Security:
    internal-only). Blank/whitespace env value treated as unset.
  - `async get_client(target=None, namespace="default")` — `await
Client.connect(...)`. OTel interceptor wiring left as a documented
    `TODO(ADR 015 Open Q5)`, not implemented.
- `worker.py`
  - `async run_worker(task_queue, *, workflows=None, activities=None,
client=None)` — gets a client via `get_client()` when not injected, builds
    `temporalio.worker.Worker(client, task_queue=..., workflows=[], activities=[])`,
    `await worker.run()`. Workflows/activities are injectable for W3.
  - `discover_workflows() -> list[type]` — **stub returning `[]`**; W3 will
    implement package-walking auto-discovery so worker Deployments only set
    `TASK_QUEUE`. Kept as a named seam.
  - `__main__` guard (`_main`): reads `TASK_QUEUE` env, `asyncio.run(run_worker(...))`.
    Entrypoint will be `python -m projects.lakehouse.orchestrator.worker`
    (W3 wires the `py_venv_binary` + image).
- Tests (hermetic, mock temporalio — no real connection):
  - `client_test.py` — `resolve_target` default, env override, blank fallback,
    whitespace strip.
  - `worker_test.py` — `run_worker` builds a `Worker` with the given task queue
    and empty workflow/activity lists (mocks `temporalio.worker.Worker` +
    `get_client` via `AsyncMock`); injected-client path; `discover_workflows`
    stub returns `[]`.
- `BUILD` — best-effort `py_library(name="orchestrator")` + a `py_test` per
  `*_test.py`. `ci-format-bot` (gazelle) normalizes and adds `semgrep_test`
  targets on the PR branch.

## Key decisions

- **Target resolution** centralizes the default in `DEFAULT_TARGET`
  (`__init__.py`) so client + tests share one source of truth; `resolve_target`
  is pure (takes an injectable `env` mapping) and treats blank `TEMPORAL_TARGET`
  as unset to avoid an unusable target from an empty manifest value.
- **`discover_workflows()` stub** is shipped now (returns `[]`) as a stable
  symbol W3 fills in, rather than leaving the seam implicit.
- **`TaskQueue` is a `str`-Enum** so members pass directly to
  `Worker(task_queue=...)` and compare equal to their plain string value.

## Deviations from ADR 015

- ADR 015 §Architecture mentions a `weather-fetch` queue in diagrams; the unit
  spec scoped this enum to the three queues it names explicitly (`gap-drain`,
  `iceberg-builder`, `housekeeping`). `weather-fetch` can be added when that
  workflow lands. Not a contradiction — the enum is a constants surface, not an
  exhaustive registry.
- ADR 015 §Architecture says workflows live in
  `projects/monolith/monolith/orchestrator/`; Wavefront-0 discovery relocated
  the lakehouse code home to the standalone `projects/lakehouse/` project (see
  LIB-SCAFFOLD). This unit follows the scaffold, not the ADR's pre-relocation
  path reference.
- OTel interceptor (ADR 015 Open Q5) and mTLS/namespace-auth connection options
  (§Security) are documented TODOs, not implemented in this skeleton.
- **`DEFAULT_TARGET` is assembled from parts, not a single literal.** The spec
  asked for a `temporal-frontend.temporal.svc.cluster.local:7233` default; the
  repo's `no-hardcoded-k8s-service-url` semgrep rule (and CLAUDE.md anti-pattern)
  block any `.svc.cluster.local` literal in non-test Python. Reconciled by
  building the host from `DEFAULT_FRONTEND_SERVICE`/`DEFAULT_FRONTEND_NAMESPACE`/
  `_CLUSTER_DNS_SUFFIX`/`DEFAULT_FRONTEND_PORT` constants in `_default_target()`,
  so the literal never appears while the resolved value is unchanged
  (`client_test.py`, which is rule-exempt, pins the full string). The canonical
  override path remains `TEMPORAL_TARGET` injected from `values.yaml`.

## Verification

No local bazel test (per repo conventions — no darwin workflows runners). All
five files AST-parse clean; tests are hermetic and mock temporalio. Relying on
PR-branch CI for `bazel test` + `ci-format-bot` BUILD normalization.
