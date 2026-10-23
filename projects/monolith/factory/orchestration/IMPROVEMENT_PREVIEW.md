# Quota improvement preview

`factory_problem_issues.improvement_preview` is a read-only first increment of
problem-issue discovery. It returns draft payloads and reasons, never creates an
issue, calls a model, reserves capacity, or changes runtime policy. The live
problem-issue tick deliberately does not call it. There is no new queue.

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
An account absent from grant telemetry abstains, including Claude on brokers
which only enumerate Codex grants.

Default eligibility requires at least 50 percent **remaining**, reset within
302400 seconds (half a week), and remaining quota at least 10 percentage points
above the fraction of the window's time remaining. Zero percent used means
100 percent remaining. The output records remaining percent, seconds until
reset, surplus percentage points, provider, grant and reset provenance. It
never interprets accounting list-price USD as actual cash charges.

`PreviewPolicy` allows tighter configurable limits within hard bounds. Default
cooldown is 24 hours; three attempts without evidence of useful completion stop
further candidates pending a usefulness review. Ticket count is not a success
metric. Repeated fingerprints are suppressed even after cooldown. A preview
cannot persist attempts: any future caller must record empty/failed attempts
in the existing audit ledger as well as successful drafts, and use its existing
write-start/reconcile protocol for uncertain GitHub outcomes.

**Why.** A quota balance is neither an instruction to invent work nor evidence
of permission to spend. Starting with a pure preview makes the decision and
its bounds reviewable before adding effects to the existing problem-issue
producer. Drafts carry that producer's marker and no `agent-ready` label.
Ordinary refinement and intake remain responsible for readiness and admission.
No deployment flag is added, and deploying this code alone enables no loop.
Live integration needs separately reviewed audit-backed cooldown/history,
current rule and goal reads, complete dedup checks and subscription admission
at dispatch time. It must retain current cash, owner and runtime controls.
