# Assess exported Stop evidence

Issue [#6564](https://github.com/jomcgi-org/homelab/issues/6564) owns operational
acceptance. This procedure and the offline checker are staged repository support.
They do not enable Stop, authorize live work, close #6564 or unblock #6256.
Local tests use synthetic records and satisfy no live check.

The independently authorized operational owner performs the opt-in session,
browser and fault steps below. This implementation task performs none of them.
Do not obtain credentials or start workloads to use the checker. Keep exports
outside git and retain failures in the existing private evidence store. Never
publish tokens, session secrets or private result bodies in a PR.

## Locate the current path

The browser uses `/private/factory/execution`, including `StopControl.svelte`,
`stop-control.js` and `session/[id]/stop/+server.js`. It posts `turn_seq` and
`dispatch_id` to `/api/agents/sessions/{session_id}/stop` in
`factory/execution/router.py`. `_stop_dispatch` checks the authenticated session
owner and exact dispatch, including retained authoritative result receipts.
`factory/execution/transport.py` relays to EmberVM's
`POST /v1/sessions/:id/interrupt`. The current router and browser have no
`stopControlEnabled` or `AGENT_SESSION_STOP_CONTROL_ENABLED` read. The retired
environment name remains in a chart test's retired-variable list.

Existing router, transport, runtime and frontend tests cover portions of this
path. They are local regression coverage. No dedicated live #6564 acceptance
harness was found in the monolith/EmberVM acceptance paths at
`53e6d067a68c20ed97a82f9684c45d23125191a1`. Reuse those exact-turn paths for an
owner-authorized run; this procedure adds no endpoint or fault switch.

Read `docs/agents/reference.md`, monolith architecture section 4 and EmberVM's
lifecycle section before execution. Recheck the current issue and deployment;
historical comments and merged #6709/#6758/#6755 do not prove live acceptance.

## Run the bounded validation

Use a single bounded run with four isolated retained sessions, one per Claude,
Codex, Muse and Pi. The normalized checker uses turn sequences 1 and 2 in each
session and one separate isolated fault session. Use fresh native sessions with
a zero cumulative baseline and a fixed model and price schedule for both turns.
Preserve raw IDs in each locator. Never change IDs to make a mismatch pass. Finish collection and
assessment within 72 hours. Fault duration is at most five minutes.

1. Before dispatch, export `verify_deployment` results for monolith and EmberVM,
   live chart versions, image/source revisions, ancestry proving the three fixes
   (or verified successor implementations), and the actual rendered Stop
   configuration. Record the Kargo live target as well as git floors. Record a
   known-good supported rollback revision and values before the first live step.
2. On each runtime, record active session/guest/turn identity, claim owner,
   dispatch count and opaque dispatch ID. Interrupt turn 1 as its owner. Confirm
   terminal `user_interrupt` with the same VM retained. Collect its native
   result, authoritative received result receipt, usage and list cost,
   reservation transition, and the billed `agent_turns.cost_usd`, `list_cost_usd`
   and `usage_json` columns. Start and complete turn 2 on that retained VM.
   Collect the same records, plus native cumulative usage/cost through each
   terminal result. Reconcile billed turn 2 against the difference between the
   two native totals. Unknown usage leaves this check incomplete. Fresh result
   IDs alone cannot rule out cumulative re-billing.
3. After turn 1 is terminal and before turn 2 dispatches, replay the same proven
   Stop identity. Require successful 202 and no additional relay, settlement,
   charge or Destroy. Once turn 2 dispatches, submit the old identity and require
   409 before turn 2 becomes terminal, without side effects. Probe non-owner
   Stop against the currently active turn 1 or 2 before its terminal result.
   Record the authenticated requester and the session's `triggered_by`; they
   must differ after trimming and lowercasing. An anonymous 403 or an already
   fenced identity is insufficient. Export before/after audit and billed-turn
   reads for each assertion on all four runtimes.
4. In Claude's reuse case, capture an actual prior background-task notification
   after successor dispatch, within 30 seconds and before its terminal result.
   Export the native result UUID, `type: result`, `origin.kind: task-notification`,
   native session ID, usage-slice IDs and the shim skip-log locator. Match the
   successor's eventual native result and usage IDs against those records; none
   may be the old notification. Reconcile billing against the interrupted turn.
   Absence of a notification does not exercise this case.
5. Use an actual browser to capture requested, confirmed interruption, completed,
   failed/unknown and completion-wins outcomes. Export a trace and screenshot per
   outcome plus matching backend exact-turn status and result records. Capture
   Stop-request timestamps for requested, failed/unknown and completion-wins.
   Requested must precede terminal evidence; a completion-wins run requires the
   request before the completed result. API-only and mocked UI runs remain
   incomplete. Failed/unknown requires a backend Stop outcome for the same
   request and exact identity: a non-success response, 503 unknown, or a status
   read proving terminal result unavailable. Include its record ID in the
   browser record. The separate fault session can supply this evidence.
6. In one retained session, deliberately hold Stop in requested state using only
   the owner's separately approved session-local method. Queue turn 2 during
   that window. Export request, queue and held-state timestamps, all reservation
   and capacity transitions through the terminal boundary, terminal result,
   settlement and successor admission. Prove no admission or release occurred
   anywhere in the requested-only interval. A quick acknowledgment does not
   exercise this case. The queued-to-terminal interval must last at least five
   seconds. Include the authorized hold method's name and raw locator, plus a
   complete reservation/capacity history from request through terminal evidence.
   Include all intervening transitions, even if the final held-state read looks
   correct. Five seconds is the offline verifier's minimum exercise window;
   it neither authorizes a delay method nor establishes operational acceptance.
7. In the separate fault session, use only a separately authorized session-local
   timeout/failed-relay method with a five-minute cleanup deadline. Record fault
   start, Stop request, failure, Destroy request, confirmed physical cessation
   and cleanup timestamps with exact identity. Record the fault's removal and
   unrelated control-session health throughout the window. No global relay hop
   outage is permitted. An already-stopped session's Destroy is insufficient.

If no safe authorized way exists to create a delayed window, prior notification
or session-local fault, leave that case incomplete. This procedure supplies no
fault injection implementation or additional authority.

## Export and check locally

Use the existing owner-authorized status, audit and log read surfaces. Relevant
records are `agent_sessions.agent_sessions`, `agent_turns`, `pending_messages`,
`capacity_reservations` and `result_receipts`; factory-owned runs also have
`swarm.factory_start` and `swarm.factory_audit`. Export complete accounting and
reservation histories for the selected identities, with query/filter, time
bounds and row counts. Include native result receipts and runtime usage events,
not only dashboard projections. Exclude secrets. Record every raw row's primary
key, result digest, log offset or trace event index in a locator.

The checker consumes a version 1 JSON manifest and local normalized records.
Normalization is manual and must preserve raw references. It makes no service
calls, creates no sessions and accepts no credentials. All files must be inside
the manifest's directory, including trace/screenshots. Hash each file with
`sha256sum`. A minimal manifest shape is:

```json
{
  "schema_version": 1,
  "source": "live_export",
  "started_at": "<UTC deployment observation timestamp>",
  "ended_at": "<UTC end timestamp>",
  "files": [{ "path": "records/deployment.json", "sha256": "<sha256>" }]
}
```

Each normalized record has `id`, `kind`, `at` (timezone required),
`source: "live_export"` and a nonempty `raw` list. Each raw reference has `path`,
`sha256` and `locator`. Raw files retain the unmodified exported evidence.
Browser trace and screenshot paths must occur in `raw`. Every normalized record
and timestamp must fall inside the run window; the deployment record is at its
start. Known costs are nonnegative finite decimal strings, never floats. Preserve
unreported native cost as null only in the fields specified below; list cost
and usage must be known.
The following fields are required in addition to those common fields:

| Kind | Required fields |
| --- | --- |
| `deployment` | `monolith`, `embervm`: each has source `revision` (40 hex), `chart_version`, `verified: true`; `contains_prs`: `[6709,6758,6755]`; `stop_configuration` describes the actual deployed path and flag presence/absence. Raw evidence includes verified ancestry. |
| Every turn-scoped record | `runtime`, `session_id`, `guest_id`, `turn_seq`, `dispatch_id`, `claim_owner`, `dispatch_count`. Preserve the complete identity on results, receipts, settlements, billing, replay and browser records. |
| `dispatch` | One per runtime and sequence 1/2, timestamp of actual admission/dispatch, session owner `triggered_by`. |
| `result` | Native `native_result_id`, reported `cost_usd` (decimal string or null when unreported), `terminal_reason`: `user_interrupt` on 1, `completed`, `end_turn` or `stop` on 2 (preserve the native reason); `vm_destroyed: false`; `list_cost_usd`; nonempty `usage` list. Each component has globally unique native `id`, matching `dispatch_id`, `observed: true`, nonempty `units` map of observed nonnegative integer counts and `list_cost_usd`. Do not invent component IDs from row counts; derive them from exact runtime events or receipt usage slices. Raw records must show the allocation and pricing. |
| `receipt` | `authoritative: true`, `result_id`, `native_result_id`, ordered `usage_ids`, `cost_usd`, `list_cost_usd`. Include received result body/digest and supersession state in raw evidence. |
| `settlement` | `receipt_id`, `state: "settled"`, same `usage_ids`, `list_cost_usd`. |
| `agent_turn` | Export of the billed `agent_sessions.agent_turns` row, with its primary key in `raw`, `cost_usd` (decimal string or null), `list_cost_usd` and unmodified `usage_json` string. Parsed usage counters must match component sums and `native_result_receipt.receipt_id` must match the authoritative receipt. No balance or posting table exists. |
| `native_totals` | Per turn: `native_session_id`, `native_result_id`, `cumulative_units` map, `cumulative_cost_usd` (decimal string or null if not reported), `cumulative_list_cost_usd`. Token-priced runtimes also provide `price_per_unit_usd`, a decimal USD-per-token map with the same keys as `cumulative_units`. Preserve native observations and pricing references in `raw`; see normalization below. |
| `export_inventory` | Per runtime: `complete: true`, `turns: [1,2]`, `record_ids` listing the twelve dispatch/result/receipt/settlement/agent_turn/native_totals records. Raw query evidence establishes that nothing was omitted. |
| `duplicate`, `stale`, `non_owner` | Per runtime, exact target identity, `http_status` 202/409/403 respectively; `additional_relays`, `additional_settlements`, `additional_billed_turns`, `additional_destroys` all zero, established by bounded before/after reads. Duplicate uses turn 1 after its result and before successor dispatch; stale uses turn 1 strictly between successor dispatch and terminal result. Non-owner uses an active turn 1 or 2 and includes `authenticated: true`, nonempty `requester` and session `triggered_by`, with distinct normalized identities. |
| `background_notification` | Claude original identity, native `native_result_id` UUID, `native_type: "result"`, `origin: {"kind": "task-notification"}`, `native_session_id`, nonempty `usage_ids` and `skip_log_locator` present among raw locators. `ignored_by_dispatch_id` is turn 2, `additional_billed_turns: 0`, `successor_terminated: false`. Timestamp must fall after successor dispatch, within 30 seconds, and before its result. Successor result/usage IDs must be disjoint. |
| `backend_stop` | Exact identity (may be the fault session), `requested_at`, `http_status`, `outcome`: failed/unknown/terminal_result_unavailable, `terminal_result_available`. Its timestamp is the response/status read. A 202 requires unknown/unavailable with `terminal_result_available: false`; a successful interruption cannot establish failed/unknown UI. |
| `browser_requested`, `browser_confirmed`, `browser_completed`, `browser_failed_unknown`, `browser_completion_wins` | Exact turn identity, `capture: "actual_browser"`, `trace` and `screenshot` raw paths, `status_dispatch_id`, `ui_outcome`: requested/confirmed/completed/unknown/completed respectively, `stop_requested_at` where specified above. For failed/unknown also include `stop_outcome_id` matching the backend Stop record, with the same request timestamp. Raw traces must show status agreement and the observed outcome. |
| `delayed_stop` | Original identity, `requested_at`, `queued_at`, `successor_dispatch_id`, `successor_admitted: false`, `reservation_held: true`, `capacity_held: true`, `held_until` equal to terminal result timestamp. `at` is a held-state read after queuing and before the terminal result; the queued-to-terminal interval is at least five seconds. `hold_method` has `name` and raw `locator`; `history_complete: true`; `reservation_history` contains at least two ordered states spanning exactly `requested_at` through `held_until`. Each state has `at`, stable nonempty `reservation_id`, raw `locator`, `reservation_held: true`, `capacity_held: true`, `successor_admitted: false`. Export every intervening transition. |
| `destroy_fallback` | Separate session and guest identity with `runtime`, `dispatched_at`, `failure`: timeout/failed_relay, `fault_started_at`, `stop_requested_at`, `stop_failed_at`, `destroy_requested_at`, `destroy_confirmed_at`, `cleaned_at`; `scope: "session"`, `cleaned: true`, `destroy_terminal: true`, `unrelated_sessions_healthy: true`, nonempty `control_session_ids`. Raw evidence covers the control sessions and whole fault window. |

For native totals, use Claude's pre-delta `total_cost_usd` (or retained
`cumulative_total_cost_usd`/interrupt cost-state) and cumulative model usage;
Claude list cost equals the native reported cost. Use Codex
`thread/tokenUsage/updated.total` for the same thread, never `last`. Muse exposes
session cumulative counters. For Pi or an event stream without cumulative
counters, sum the complete native usage events from session start through each
result, deduplicated by native event ID. Include the complete event inventory
and aggregation locators. Never derive native totals from the billed turn rows.
Unknown, reset or missing counters leave this bounded run incomplete.

Map native counters into the persisted `usage_json` token fields, preserving
raw field paths and cache allocation. Token-priced runtimes must export the
fixed deployed model's per-unit prices alongside the native observations. The
checker recomputes cumulative list cost as the sum of native counts times those
prices, then compares each turn's billed list cost and units with the native
delta. Preserve unreported `cost_usd` as null and compare it to result/receipt
values; do not invent a reported cost for Codex. Raw review must confirm the
pricing schedule and native-to-persisted counter mapping, including disjoint
cache categories. Mixed models or counter resets require a separate supported
normalization; do not flatten them into this fixed-model bundle.

Run from the repository root:

```sh
python3 projects/monolith/scripts/stop_acceptance.py /path/to/private-export/bundle.json
# Local synthetic verifier tests only:
PYTHONPATH=projects/monolith python3 -m pytest -q projects/monolith/scripts/stop_acceptance_test.py
# Linux CI target: //projects/monolith:stop_acceptance_test
```

Exit 1 and `status: incomplete` mean evidence is missing or inconsistent.
Exit 0 and `status: consistent` mean the normalized records passed offline
consistency checks. `operational_acceptance` always remains
`pending_raw_evidence_review`. Hashes detect local drift; they do not establish
authenticity. The operational owner must compare normalized values, complete
export queries, exact identities and assertions to raw records. Labeling a
fixture `live_export` cannot make it live evidence. Never submit test fixtures
as an acceptance bundle. Preserve contradictory records and report a failed
check rather than deleting evidence to obtain a consistent result.

## Roll back on failure

Stop validation on any failed acceptance check, retain evidence and have the
authorized landing/operational owner roll back through reviewed GitOps delivery.
Do not accept the run until rollback is verified. This worker performs no rollback.

The original issue required disabling the historical flag on failure. HEAD no
longer provides that switch. The owner must inspect the deployed chart before
choosing a lever: use the real switch only if the deployed version still reads
it. Otherwise revert the enabling/runtime change or lower the deployment pin to
the recorded supported known-good chart and compatible values through a PR,
required Linux CI, independent review and merge queue. Pin floors live in
`projects/gke-apps/monolith/application.yaml` and
`projects/gke-apps/embervm/application.yaml`; overlay values live under each
service's `deploy/`. CI owns chart increments, image pushes and forward pins.

Kargo owns the live hub revision, so git floors alone do not prove rollback.
The landing owner must follow the existing Kargo rollback promotion route for
`kargo-monolith:prod` / `kargo-embervm:prod`, preserving admission and approval
gates, and verify the expected revisions with `verify_deployment`. See monolith
architecture's `verify_deployment` and `kargo_promote` contracts and
`docs/agents/reference.md`. Do not bypass an unavailable rollback route; keep
acceptance failed/incomplete and report the blocked rollback to its owner.
Keep #6564 open and #6256 blocked until all original checks pass or Joe explicitly
changes acceptance. No ownership or hold changes belong to this procedure.
