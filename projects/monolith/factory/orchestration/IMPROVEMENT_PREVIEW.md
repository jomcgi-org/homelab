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

`PreviewPolicy` allows tighter configurable limits within hard bounds for local
previews. The integrated source uses the fixed conservative policy with one
draft per batch. The pure preview cannot persist attempts; the integrated
source always loads and writes the existing ledger itself. History is capped
at 1000 attempts and refuses on overflow rather than discarding old evidence.

**Why.** A quota balance is neither an instruction to invent work nor evidence
of permission to spend. A reviewed, bounded batch makes discovery useful
without enabling a speculative model loop. Reusing the existing issue writer
preserves its ambiguous-outcome fence, instead of introducing another queue.
A later autonomous scout must retain these gates and use verified subscription
admission before any model execution; this change makes no such execution.
