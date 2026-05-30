# SVC-DISPATCHERS — NATS -> Temporal workflow dispatchers + image

**Unit:** SVC-DISPATCHERS (Wavefront 4)
**ADR:** [agents/016 — NATS as Canonical Event Stream](../../decisions/agents/016-nats-canonical-event-stream.md) §"Workflow dispatchers" · [agents/017 — Domain Event Schema](../../decisions/agents/017-domain-event-schema.md) · [agents/015 — Temporal Orchestration Substrate](../../decisions/agents/015-temporal-orchestration-substrate.md)
**Branch:** `feat/lakehouse-w4-dispatchers`
**Classification:** purely-additive — one new dir `projects/lakehouse/dispatchers/` plus a one-line insert into the auto-generated `bazel/images/BUILD` push list (exactly what `//bazel/images:generate-push-all` emits; pre-applied so the FIRST CI run pushes the image and the format check sees no diff).
**Scope:** AUTHOR-ONLY. Nothing is wired to a Deployment by this unit; the dispatcher Deployment lives in the sibling W4-CHART unit (which references this image by repo string). The gap dispatcher is a **shadow / parallel** definition — production cutover from the live orchestrator is out of scope (see SCOPE GUARD below).

## What shipped

A `monolith-dispatchers`-style process (ADR 016 §"Workflow dispatchers": small
adapters translating NATS events into `start_workflow` calls).

- `projects/lakehouse/dispatchers/__init__.py` — the `Dispatcher` dataclass
  (`subject`, `durable`, async `handle(envelope, temporal_client)`, optional
  `event_type` filter) + a `pkgutil.iter_modules` loader exposing
  `all_dispatchers()` that aggregates each submodule's module-level `DISPATCHERS`
  list. **New dispatcher = a new file, no shared-list edit** — mirrors the
  `orchestrator.workflows` / `orchestrator.schedules` loaders.
- `projects/lakehouse/dispatchers/gap_ready.py` — `DISPATCHERS` entry for subject
  `events.knowledge.gap`, filter `event_type == "created"`. Handler starts
  `GapDrainWorkflow` (referenced by **type-name string**, never imported) with
  deterministic ID `gap-drain-{entity_id}` on task queue `gap-drain`, swallowing
  `WorkflowAlreadyStartedError` as an idempotent no-op.
- `projects/lakehouse/dispatchers/artifact_ready.py` — `DISPATCHERS` entry for
  subject `events.serving.artifact-ready`. **Minimal stub** (logs only): the
  quack-server pod already self-subscribes to this subject and performs its own
  `ATTACH OR REPLACE` (`quack-server/server.py:run_swap_consumer`, durable
  `quack-serving-swap`), so this is a secondary/optional reaction hook with a
  **distinct durable** (`artifact-ready-dispatcher`) so JetStream fans out to both
  consumers independently. Deliberately does NOT start a swap workflow (no
  double-swap).
- `projects/lakehouse/dispatchers/run.py` — entrypoint
  (`python -m projects.lakehouse.dispatchers.run`). Connects `NatsClient` +
  Temporal (`orchestrator.client.get_client`), opens a durable pull consumer per
  dispatcher, and polls all in a graceful loop. Message disposition mirrors the
  quack consumer: malformed envelope -> `term`; filtered-out event -> `ack`;
  handler success -> `ack`; handler raises -> left un-acked for JetStream
  redelivery (the per-subscription loop logs and continues so one bad message
  can't kill the loop).
- `projects/lakehouse/dispatchers/BUILD` — `py_library` (gazelle-managed) +
  `py_venv_binary` (`name = "main"`, `main = run.py`) + hand-written `py3_image`
  (`repository = "ghcr.io/jomcgi/homelab/projects/lakehouse/dispatchers"`,
  multiarch, non-root 65532, PYTHONPATH = workspace root, modeled on
  `projects/lakehouse/image`) + `py_test` + semgrep targets.
- `projects/lakehouse/dispatchers/dispatchers_test.py` — hermetic tests
  (fakes for NATS client/subscription/message + Temporal client).
- `bazel/images/BUILD` — `//projects/lakehouse/dispatchers:image.push` inserted
  into the sorted `push_all` list (auto-generated; pre-applied).

## gap_ready dispatch design

1. Producer publishes a `gap` `created` event to `events.knowledge.gap`
   (ADR-017 envelope, `Nats-Msg-Id = {entity_id}-v{event_version}`).
2. The run loop's durable pull consumer (`gap-drain-dispatcher`) fetches it,
   decodes it to an `EventEnvelope`, applies the dispatcher's `event_type ==
"created"` filter (`updated`/`processed`/`tombstoned` are acked + ignored).
3. `handle_gap_created` calls
   `temporal_client.start_workflow("GapDrainWorkflow", envelope.payload,
id="gap-drain-{entity_id}", task_queue="gap-drain")`.
4. If a drain for that gap is already running, Temporal raises
   `WorkflowAlreadyStartedError`, which is **swallowed** — the three independent
   trigger paths (this dispatcher / cron sweep / manual) converge on one
   deterministic workflow ID; whichever fires first wins (ADR 015 §"Workflow
   identity = work identity").
5. On success the message is acked. Net effect: at-least-once NATS delivery +
   workflow-ID dedup = exactly-once workflow start.

The workflow is referenced by **type-name string** (no import of the workflow
class), so the dispatcher carries no workflow/activity dependency and stays a
~30-line adapter (ADR 016).

### Note on the `temporalio` exception path

The spec phrases this as `temporalio.client.WorkflowAlreadyStartedError`, but in
the pinned `temporalio==1.27.2` the symbol is **not** re-exported from
`temporalio.client` — it lives in `temporalio.exceptions` (same as the W3
`gap_drain` workflow's import). The handler imports it from there; verified
against the pinned wheel.

## SCOPE GUARD (deliberate — production cutover is a FOLLOW-UP, not this run)

The gap dispatcher is a **parallel / shadow** definition, reacting to (and
triggering) the W3 shadow `GapDrainWorkflow`. Production gap dispatch still flows
through the existing live orchestrator (`projects/agent_platform/orchestrator` /
`knowledge/research_handler`). Wiring this NATS->Temporal path as the production
gap pipeline — and deploying the dispatcher pod (W4-CHART) — is a deliberate
follow-up unit. Nothing here competes with the live pipeline.

## Validation

No local `bazel test` (Mac has no `workflows`-pool runner). Validated by:

- **Hermetic test run in a throwaway venv** with the _pinned_ `temporalio==1.27.2`
  - `pydantic` + `nats-py`: **19/19 passed**. Asserts gap-created event ->
    `start_workflow("GapDrainWorkflow", id="gap-drain-<id>", task_queue="gap-drain")`,
    `WorkflowAlreadyStartedError` swallowed, `all_dispatchers()` discovers both,
    event_type filter, artifact-ready stub never touches Temporal, malformed -> term,
    handler-error -> redelivery, end-to-end run() wiring with fakes.
- `ruff check` + `ruff format --check`: clean.
- Push-list insert position confirmed via `LC_ALL=C sort` (matches what
  `generate-push-all.sh` emits).
- CI "Test and push" (`bazel test //...` + image push on main) + the `py3_image`
  macro's `image_config_test` are the real validation.

## Status

- Implementation: complete
- CI / Test / Push-images: <!-- filled in post-merge -->
