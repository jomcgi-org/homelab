# Quota improvement preview

The pure `factory_problem_issues.improvement_preview` returns draft payloads
and reasons without effects. The opt-in `quota_improvement` source can now
persist one reviewed candidate and create its ordinary GitHub issue through
the existing problem-issue producer. Both the overall producer and the new
source default off. No scouting/model loop or competing queue is added.

An authenticated standing factory operator uses the existing private factory
router, with unchanged permission checks:

1. Read `GET /api/swarm/factory/improvements/context`. This loads broker grant
   telemetry, current factory policy/version, active goals from their existing
   database table, active/queued/escalated owners, and bounded complete open
   issue/PR listings. A fresh, complete existing intake sweep must report zero
   candidates in both lanes and no active owners. Missing/partial/stale intake
   evidence abstains, even when capacity is idle.
2. Review the rules, goals and all open work, including semantic duplicates
   whose titles differ. Submit `POST /api/swarm/factory/improvements` with that
   `review_digest`, `rules_reviewed: true`, `dedup_reviewed: true`, verified
   `subscription_caps` and at most 20 evidence-backed `findings` (fields below).
   The server reloads context and rejects a changed digest. Caller-supplied
   quota, backlog, goals and history are not accepted by this endpoint.
3. With factory enabled and `problem_issues.enabled` plus
   `problem_issues.sources.quota_improvement` explicitly enabled, submission
   records an `improvement_attempt` and at most one `improvement_candidate` in
   the existing audit ledger. No-findings and duplicate attempts also count.
   Concurrent submissions share the existing factory lock. Defaults are one
   batch per 24 hours and at most three attempts without useful completion.
   Missing prerequisites (stale quota, ready backlog, incomplete context) defer
   discovery without consuming the scouting trial.
4. The existing problem-issue tick rechecks the live context and quota, then
   uses its single writer, daily cap, fingerprint and ambiguous-write
   reconciliation. A candidate expires after 900 seconds. Rules/goal/owner
   changes and stale evidence refuse it. Issues receive no readiness label;
   existing refinement and intake own admission. No model/provider calls are
   made by scouting submission or issue creation.

Usefulness feedback is derived from a succeeded non-advisory delivery receipt
plus its `owned_pr_merged` evidence. An issue write, refinement success or
closed ticket alone never resets the three-attempt stop. Existing audit history
also suppresses repeated fingerprints across restarts. The status endpoint's
existing problem-issues activity includes attempts and candidates.

Activation belongs to the runtime owner after the bounded behavior and current
rules have been reviewed. This change does not enable the source, alter cash
caps, change providers, restart factory/KG, or modify Luna capture. The endpoint
can create issues only after opt-in; it never authorizes model execution.

For a local, manual preview of a reviewed snapshot:

```sh
PYTHONPATH=projects/monolith python -m factory.orchestration.factory_improvement_preview < snapshot.json
```

The input is a JSON object with these fields. The test fixture in
`factory_improvement_preview_test.py` is a complete synthetic example.

| Field | Contract |
| --- | --- |
| `repo` | `jomcgi-org/homelab` only |
| `captured_at` | Aware timestamp of the reviewed snapshot, at most 900 seconds old |
| `intake_complete`, `dedup_complete`, `history_complete`, `rules_reviewed` | Explicit booleans attesting the corresponding reads are complete and current; absence is unknown |
| `ready_issue_numbers` | Eligible delivery candidates from the existing intake evaluator, in its priority order, after exclusions, linked PR checks and active receipts. Any candidate suppresses discovery |
| `quota` | Raw broker quota payload including `available`, `grants_complete`, `fetched_at` and per-account `grants`; never the provider summary |
| `subscription_caps` | Map keyed by grant. Each entry needs `subscription_only: true`, `overage_disabled: true` and a nonempty `evidence` reference to verified cap configuration. This is a reviewer attestation, not a cap reservation |
| `active_goal_ids` | IDs from the currently declared factory goals, not goals invented by a scout |
| `inventory` | Complete normalized dedup inventory of open issues, PRs and active owners, each with a canonical `problem_key`. A reviewer must map equivalent symptoms to the same key; titles alone are insufficient |
| `history` | Complete prior discovery attempts, including empty/failed attempts, each with `attempted_at` and optional `fingerprint`. A useful completion needs `outcome: useful_completion`, `merged_pr` and `acceptance_evidence` |
| `findings` | Evidence collected by bounded manual scouting, at most the first 20 examined; fields below |

Each finding needs a stable `problem_key`, scoped `title`, one of `bug`,
`failing_test`, `performance`, `reliability`, `goal_improvement` as `kind`,
`evidence` (commit/test output or measurement reference), `reproduction`,
`benefit`, `acceptance_tests`, proposed `owner`, active `goal_id`, `ready: true`
and a fresh aware `observed_at`. Text presence is validated here; its truth and
semantic deduplication remain reviewer responsibilities. Unknown/incomplete
inventory must not be represented as an empty inventory. Preview inputs are not
an authenticated authorization interface.

Broker `resets_at` and `window_minutes` come from provider response headers.
They are reported reset evidence, not guessed calendar boundaries. Imported
estimates must carry `reset_source: estimate` and abstain. Missing, expired,
contradictory or stale windows abstain. No inference from a window's name is
made. All windows on an account must be usable and nonempty; the longest is its
budget. Accounts are never pooled, and the provider summary is never a fallback.
Explicit per-grant broker `conserve`, blocked or unknown pacing also abstains;
a provider headline never overrides a grant. Each shorter window must retain
at least 10 percent headroom and must not be more than five percentage points
ahead of its time-based consumption pace. A spare weekly budget cannot override
a binding burst window. The short-window reserve can only be tightened.
An account absent from grant telemetry abstains, including Claude on brokers
which only enumerate Codex grants.

Default eligibility requires at least 50 percent **remaining**, reset within
302400 seconds (half a week), and remaining quota at least 10 percentage points
above the fraction of the window's time remaining. Zero percent used means
100 percent remaining. The output records remaining percent, seconds until
reset, surplus percentage points, provider, grant and reset provenance. It
never interprets accounting list-price USD as actual cash charges.

## Generation policy and activation

The 24-hour default is a pilot limit on novel scouting, allowing useful-delivery
feedback to arrive before more speculative work is added. It never changes
existing intake, execution concurrency, task budgets or backlog throughput.
There is no minimum daily issue target. As useful completions establish value,
the runtime owner can tune the existing `problem_issues` policy:

| Key | Default | Allowed |
| --- | --- | --- |
| `improvement_limits.cooldown_seconds` | 86400 | 3600 to 604800 |
| `improvement_limits.max_findings` | 20 | 1 to 20 |
| `improvement_limits.max_unproven_attempts` | 3 | 1 to 3 |
| `max_per_24_hours` (shared writer) | 3 | 1 to 100, shared with other problem sources |

One candidate per batch remains a hard bound. Both the scouting cooldown and
the existing shared writer cap apply, so shortening the cooldown alone cannot
exceed the writer cap. Trial and cooldown evaluation read the latest 20 attempts (the trial maximum is
three). Duplicate checks search the full durable ledger; there is no lifetime
attempt cap or deletion of old evidence. Pure previews additionally allow
tighter quota thresholds through `PreviewPolicy`; the live quota freshness,
reset, grant pacing and burst safeguards are unchanged by generation tuning.

A stopped unproductive trial requires a deliberate review, not endless retries.
The context response exposes durable `improvement_history` including audit IDs.
After reviewing why prior attempts failed and what new evidence makes further
scouting useful, the operator can configure
`improvement_review: {"through_audit_id": <exact attempt ID>, "reason": "<evidence and rationale>"}`.
The ID must exist and name an improvement attempt, preventing a future watermark
from exempting later attempts. Existing policy configuration records the reason
and watermark in its audit. This only restarts the consecutive-attempt trial;
it does not clear cooldowns, fingerprints or GitHub write outcomes.

These are verified repository routes, available only after this code rolls out:

- `GET /api/swarm/factory` reads the current full policy and status.
- `GET /api/swarm/factory/improvements/context` is read-only and returns current
  rule/goal/inventory evidence plus its review digest and durable history.
- `POST /api/swarm/factory/control`, `action: configure`, `policy: <complete
  preserved policy with the reviewed changes>` is the existing authenticated
  policy path. Enable both `problem_issues.enabled` and the
  `sources.quota_improvement` entry there. Configuration records an audit and
  changes policy; preserve unrelated settings and the operator's runtime state.
- `POST /api/swarm/factory/improvements` accepts only the bounded request fields
  described above. It records eligible scouting attempts and at most one source
  candidate. It does not itself write GitHub, change policy, or run a model.
  The existing producer tick later makes at most one GitHub write per tick.

The currently exposed MCP `factory_control` tool supports runtime controls,
**not policy configuration or these new batch routes**. Do not invent MCP
arguments, guess a service hostname, acquire credentials, or bypass access to
invoke them. Use the existing authenticated private operator API/UI when
available after rollout; otherwise hand off activation as blocked on that
supported surface. No such activation was performed by this change.

**Why.** A quota balance is neither an instruction to invent work nor evidence
of permission to spend. A reviewed, bounded batch makes discovery useful
without enabling a speculative model loop. Reusing the existing issue writer
preserves its ambiguous-outcome fence, instead of introducing another queue.
The automatic scout below retains these gates and uses verified subscription
admission before any model execution; the batch API itself makes no such execution.

## Automatic scout: one bounded trial per charter and generation

The default-off `problem_issues.improvement_scout` integration collects evidence
automatically on the existing conductor tick after ordinary intake/admission.
It creates one durable `advisory-diagnosis` receipt with a `scout_claim` audit,
uses the existing graph and execution admission, and submits reviewed findings
to the same candidate ledger and single issue writer. There is no added timer,
queue, provider, runtime flag change, or direct GitHub writer.

Configuration requires a real, open, dedicated scouting charter issue:

```json
{
  "improvement_scout": {
    "enabled": false,
    "issue_number": 0,
    "subscription_caps": {}
  }
}
```

Replace zero with the reviewed charter issue number and provide per-account
subscription-only/no-overage evidence using the same cap attestation structure
as reviewed batches. The charter must have `needs-thought`, a nonempty scoped
body, no assignee, and no `agent-ready`, `human`, `needs-human`, or
`security-finding` label. Do not add it to the normal delivery allowlist or use
an unrelated goal issue. Both the existing producer and quota-improvement source
must also be enabled; ordinary factory/repository/advisory admission controls
still apply. Enabling requires the existing authenticated configuration route.
No charter has been created and none of these settings has been enabled here.

The first increment admits **at most one scout per charter and policy
generation**. It never automatically requeues a settled receipt or increments
policy generation. The operator reviews the trial before authorizing another;
this limit applies only to novel scouting and leaves existing useful backlog
execution unchanged. Existing cooldown, finding limits and unproductive-trial
limits also apply across generations. Settled claims short-circuit without
repeating network scouting every tick.

Each trial has at most two nodes: one evidence collector and a separate-session
reviewer, each with one attempt, a maximum 300-second turn timeout, and the
existing `sol` Codex subscription model. Both nodes are read-only apart from
the declared result artifact. There is no model/provider fallback, broad test
run, dependency installation, ticket-writing agent, or implementation work in
the scout. Generic funding judges cannot extend this trial or add nodes. Findings must include the existing reproducible evidence, benefit,
acceptance, ownership and active-goal fields. The review binds both the exact
collection digest and fresh rules/goals/open-work context. Server verification
requires successful, distinct collection/review sessions before candidate
submission. Empty, failed and rejected attempts count toward the trial stop;
only useful merged delivery resets that feedback, never ticket count.

Codex currently uses ChatGPT subscription authentication in
`runtimes/claude/shim.py` and the existing broker-backed sidecar. Since the
sidecar chooses the account and may fail over, the scout requires **every Codex
grant in complete broker telemetry** to be fresh, individually eligible and
covered by cap evidence. One account's `use_more` never overrides another's
`conserve`. These checks run before graph dispatch and through the existing
live transport fence on wake/retry immediately before execution. Task/capacity
reservations alone do not constitute subscription eligibility. The executor uses
a small dependency-isolated fence and rechecks durable controls after broker I/O
to reject intervening pauses, policy changes or competing owners. Missing or
changed evidence defers; no paid API fallback is introduced.

Ownership checks exclude only the exact receipt backed by the scout claim;
new ready backlog and all other owners still block scouting/publication. The
writer revalidates context after review. Durable attempt/settlement history
handles a crash after submission without creating a second candidate.

Rollout remains with the runtime owner. Land and verify this separate increment,
review the charter and current policy/cap evidence through supported surfaces,
then enable a single trial only with fresh complete intake and quota telemetry.
Observe `scout_claim`, `scout_review`, `scout_settled`, `improvement_attempt` and
existing issue-write outcomes. Broader recurring scouting is not enabled by
this one-trial integration and must retain the same ownership/admission gates.
