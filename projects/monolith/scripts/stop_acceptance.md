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
session and one separate isolated fault session. Use fresh sessions with that
shape, or preserve an explicit raw-to-normalized sequence mapping in each raw
locator. Never change raw IDs to make a mismatch pass. Finish collection and
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
   reservation transition, complete billing postings and account balance delta.
   Start and complete turn 2 on that retained VM. Collect the same records.
   Reconcile per-component costs and balances across both turns. Unknown usage
   leaves this check incomplete. Counting two turn rows is insufficient.
3. After turn 1 is terminal and before turn 2 dispatches, replay the same proven
   Stop identity. Require successful 202 and no additional relay, settlement,
   charge or Destroy. Once turn 2 dispatches, submit the old identity and require
   409 without side effects. Verify non-owner 403 without side effects on all
   four runtimes. Export before/after audit and ledger reads for each assertion.
4. In Claude's reuse case, capture an actual prior background-task notification
   within 30 seconds of successor dispatch. Show its original dispatch identity,
   that the successor ignores it, and the successor's eventual successful result
   on the retained VM. Reconcile the successor's usage components and billing
   against the notification and interrupted turn. Absence of a notification does
   not exercise this case.
5. Use an actual browser to capture requested, confirmed interruption, completed,
   failed/unknown and completion-wins outcomes. Export a trace and screenshot per
   outcome plus matching backend exact-turn status and result records. Capture
   Stop-request timestamps for requested, failed/unknown and completion-wins.
   Requested must precede terminal evidence; a completion-wins run requires the
   request before the completed result. API-only and mocked UI runs remain
   incomplete. Failed/unknown requires an actual relay error or unknown status.
6. In one retained session, deliberately hold Stop in requested state using only
   the owner's separately approved session-local method. Queue turn 2 during
   that window. Export request, queue and held-state timestamps, all reservation
   and capacity transitions through the terminal boundary, terminal result,
   settlement and successor admission. Prove no admission or release occurred
   anywhere in the requested-only interval. A quick acknowledgment does not
   exercise this case.
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
start. Costs are nonnegative finite decimal strings, never floats or null.
The following fields are required in addition to those common fields:

| Kind | Required fields |
| --- | --- |
| `deployment` | `monolith`, `embervm`: each has source `revision` (40 hex), `chart_version`, `verified: true`; `contains_prs`: `[6709,6758,6755]`; `stop_configuration` describes the actual deployed path and flag presence/absence. Raw evidence includes verified ancestry. |
| Every turn-scoped record | `runtime`, `session_id`, `guest_id`, `turn_seq`, `dispatch_id`, `claim_owner`, `dispatch_count`. Preserve the complete identity on results, receipts, settlements, ledger, replay and browser records. |
| `dispatch` | One per runtime and sequence 1/2, timestamp of actual admission/dispatch. |
| `result` | `terminal_reason`: `user_interrupt` on 1, `completed`, `end_turn` or `stop` on 2 (preserve the native reason); `vm_destroyed: false`; `list_cost_usd`; nonempty `usage` list. Each component has globally unique native `id`, matching `dispatch_id`, `observed: true`, nonempty `units` map of observed nonnegative integer counts and `list_cost_usd`. Do not invent component IDs from row counts; derive them from exact runtime events or receipt usage slices. Raw records must show the allocation and pricing. |
| `receipt` | `authoritative: true`, `result_id`, ordered `usage_ids`, `list_cost_usd`. Include received result body/digest and supersession state in raw evidence. |
| `settlement` | `receipt_id`, `state: "settled"`, same `usage_ids`, `list_cost_usd`. |
| `ledger` | `balance_before_usd`, `balance_after_usd`, complete `postings` list. Each posting has `usage_id`, `receipt_id`, `settlement_id`, `list_cost_usd`. Include every account-affecting posting in the window; unexplained charges or reversals remain incomplete. |
| `export_inventory` | Per runtime: `complete: true`, `turns: [1,2]`, `record_ids` listing the ten dispatch/result/receipt/settlement/ledger records. Raw query evidence establishes that nothing was omitted. |
| `duplicate`, `stale`, `non_owner` | Per runtime, original identity, `http_status` 202/409/403 respectively; `additional_relays`, `additional_settlements`, `additional_postings`, `additional_destroys` all zero, established by bounded before/after reads. |
| `background_notification` | Claude original identity, `ignored_by_dispatch_id` of turn 2, `additional_postings: 0`, `successor_terminated: false`. |
| `browser_requested`, `browser_confirmed`, `browser_completed`, `browser_failed_unknown`, `browser_completion_wins` | Exact turn identity, `capture: "actual_browser"`, `trace` and `screenshot` raw paths, `status_dispatch_id`, `ui_outcome`: requested/confirmed/completed/unknown/completed respectively, `stop_requested_at` where specified above. Raw traces must show status agreement and the observed outcome. |
| `delayed_stop` | Original identity, `requested_at`, `queued_at`, `successor_dispatch_id`, `successor_admitted: false`, `reservation_held: true`, `capacity_held: true`, `held_until` equal to terminal result timestamp. `at` is a held-state read after queuing and before the terminal result; raw interval history proves the full hold. |
| `destroy_fallback` | Separate exact session identity, `failure`: timeout/failed_relay, `fault_started_at`, `stop_requested_at`, `stop_failed_at`, `destroy_requested_at`, `destroy_confirmed_at`, `cleaned_at`; `scope: "session"`, `cleaned: true`, `destroy_terminal: true`, `unrelated_sessions_healthy: true`, nonempty `control_session_ids`. Raw evidence covers the control sessions and whole fault window. |

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
