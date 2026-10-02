# Factory outcomes report

The manual report for #6716 measures money and elapsed time per mature positive
outcome, grouped by task class and initiating model. Contribution rows expose
every role and model used. It supplies the factory half of the
jomcgi-agent-index (#6717); model-bench supplies the controlled comparison.

```sh
psql "$DATABASE_URL" -X \
  -v cohort_start='2026-09-07T00:00:00Z' \
  -v cohort_end='2026-10-02T00:00:00Z' \
  -v as_of='2026-10-02T00:00:00Z' \
  -f projects/monolith/factory/outcomes_report.sql
```

Use PostgreSQL 16 or later and a read-only connection, such as the existing
CNPG hot standby. The script keeps `SET default_transaction_read_only = on`.
It creates no tables or views and performs no writes. There is no scheduled
collection. Operational acceptance remains on #6716 until the report's query
cost and sampled records have been checked on the standby.

## Cohort and evidence bounds

There are exactly three report parameters: `cohort_start`, `cohort_end`, and
`as_of`. Supply explicit UTC timestamptz literals, with `Z` or `+00:00`.
The header echoes all three bounds before the four sections.

Each non-null `factory_receipt.task_id` is selected once by receipt
`created_at` in `[cohort_start, cohort_end)`, also before `as_of`. Receipts
without an implementation attempt remain in the cohort. The initiating model
is the model of the earliest implement-role run, ordered by `created_at` and
run ID, with `none` for no implementation and `unknown` for a missing model.
Run model falls back from `swarm_node_run.model` to `pin_json.model`.

All runs and starts of those tasks created before `as_of` are included,
regardless of when the attempt falls relative to the cohort bounds. This
includes failures, retries, planning and conductor work, funding, corrections,
reviews, integration, investigation, and stronger-model attempts. Tasks outside
the cohort contribute nothing. Audits and turns at or after `as_of` are
excluded. A run finishing at or after the cutoff has unknown attempt outcome
and no completed duration. A settlement at or after the cutoff is not terminal.

The report uses existing landing and revert audits without changing their
writers. Malformed JSON becomes empty evidence through PostgreSQL 16's
`pg_input_is_valid`; it cannot establish success. Each audit kind is collapsed
to one row per task before joining. The earliest `merged` audit defines the
delivery. The earliest matching `merge_ci` row defines its final CI evidence.
Repeated audit rows cannot multiply tasks or costs.

## Mutually exclusive task states

Predicates are evaluated in the following order, using evidence before `as_of`:

| State | Predicate |
| --- | --- |
| `reverted` | Any `reverted` audit for the task. This wins over every other state. |
| `ci_failed` | A `merge_ci` audit with conclusion `failure`, a PR number and a valid nonempty 40-character head SHA, matching the selected merge delivery when one exists. |
| `positive` | A `merged` audit, matching PR-number `merge_ci` with a valid head SHA and conclusion `success`, matching PR-number and merge-commit-SHA `revert_window_closed`, no reverted audit, and merge audit time plus seven days at or before `as_of`. |
| `pending_maturity` | A merged delivery with successful CI that does not meet the completed clean-window predicate. |
| `unknown` | A merge with missing, malformed, mismatched, pending, `none`, or `unknown` CI evidence, or a historical merge without a landing merge audit. |
| `failed` | No merge and `swarm_task.settled_at < as_of`. This includes terminal advisory work without a repository delivery. |
| `censored` | Neither merged nor settled before `as_of`. |

For a merged task, CI evidence must name the same PR as the selected delivery
and be recorded at or after its merge. A clean-window audit must name that PR
and its nonempty merge commit SHA and be recorded at least seven days after the merge. The
separate maturity predicate also prevents early or immature positives. Pending,
unknown, and censored tasks never count as positive.

Historical merges are linked through existing `finish_task` or `delivery_ready`
audit `evidence.pr_url`, or a completed run artifact's `value.pr_number` or
top-level `pr_number`, to `observability.merged_prs`. That snapshot contains
merged PRs only; its `merged_at` must precede `as_of`. A historical merge has no
clean-window proof. Merges lacking any stored task-to-PR reference cannot be
identified by this report and need manual reconciliation.

## Cost bounds and time

Task sessions are linked by every run's `session_id`, every start's
`session_id`, `swarm_task.session_id`, and the task ID in a session name shaped
`factory:<task_id>:...`. Session links are deduplicated with `UNION`. Each
turn ID is counted once per task even if its session is reached several ways.
All priced and unpriced turns created before `as_of` are included.

- `list_usd` sums `agent_turns.list_cost_usd`. It is the observed lower bound.
  `unpriced_turns` counts null list prices, including error turns with no usage.
- `settled_usd` sums `factory_start.cost_usd` for terminal statuses
  `succeeded`, `failed`, and `cancelled` whose `updated_at` precedes `as_of`.
- `exposure_usd` sums `GREATEST(max_cost_usd, COALESCE(cost_usd, 0))` for
  `reserved` and `uncertain` starts, matching the factory ledger's committed
  cost for unresolved reservations. Starts updated at or after the cutoff
  conservatively retain their whole reservation. A pre-cutoff terminal start
  with null cost keeps its `max_cost_usd` as exposure, because the ledger
  books the ceiling for unknown usage (stranded guests settle with null cost
  deliberately: unknown, not zero). Starts whose `accounting_basis` proves the
  attempt never reached a model (`no_model_post`, `capacity_denied`,
  `no_session_created`) commit nothing and contribute zero. Exposure is a
  reservation ceiling, not measured spend.
- `ledger_upper_usd = settled_usd + exposure_usd`. Coverage separately reports
  terminal starts with missing settled costs, excluding the proven-free bases;
  unreconciled null costs keep their reservation inside the upper bound, so a
  nonzero missing count means unknown usage is bounded rather than absent. The
  ledger and list-price measures have different accounting bases; the report
  does not force one to exceed the other.

`usd_per_positive_lower` and `usd_per_positive_upper` divide cohort totals by
positives. Failed and other nonpositive tasks remain in both numerators.
Unpriced turns and reservation exposure are always displayed beside the ratios.
Zero positives produce SQL `NULL`, never zero or division errors.

Task elapsed time starts at receipt creation. For a merged task it ends at the
merge audit time, or the stored PR merge time for an identified historical
merge. For an unmerged terminal task it ends at `swarm_task.settled_at`.
`hours_per_positive` divides the sum across all terminal tasks, including
failed tasks, by positives. It excludes censored elapsed time and the seven-day
maturity wait. `p50_hours_to_merge` is a supporting median across merged tasks.
Contribution `minutes` sums completed run durations; overlapping runs can
overlap in wall time and this is separate from task elapsed time.

## Reading the sections

Each section is one SELECT ending in a semicolon, preceded by exactly one
`\echo '== <Section name>'` marker. CTEs repeat so fixture tests can split
sections and bind the three parameters independently. There are no temporary
objects, functions, or psql commands between sections.

1. **Task outcomes by task class and initiating model:** all state counts,
   positive counts, cost brackets, elapsed-time ratios, and supporting rates.
   Only this section's task counts sum directly to the cohort total.
2. **Contributions by task class role and actual model:** all attempts,
   judged attempt success, distinct tasks, list prices, unpriced turns, ledger
   bounds, and completed minutes. Role is the node-key prefix before `_`, with
   `conductor_funding*` mapped to `funding`. Attempts use their dispatched
   model. Priced and unpriced turns use their recorded actual model, falling
   back to the session owner's run/start model. Reservations use the start
   model. A task that switches models contributes to each observed model.
3. **Difficulty bands:** task class, initiating model, dimension, and band,
   including costs and positive ratios. Dimensions are independent slices,
   so task counts repeat across dimensions.
4. **Coverage:** unknown CI, historical unjudged merges, pending maturity,
   unpriced turns, unsettled reservations, missing settled costs, censored
   tasks, missing size/file/label metadata, missing initiating models, and
   unknown first-pass CI and escalation evidence.

Contribution task counts are non-additive across models and roles. A reused
session's turns are attributed to its earliest run; otherwise its earliest
start owns it. A task-level conductor session without a run/start owner uses
`conductor`. Unmatched sessions and starts use `unassigned`. Cost-only rows can
have zero node attempts. These attribution rules prevent duplicated money
while retaining turns and reservations without a corresponding run.

## Difficulty bands

Merged PR metadata comes from `observability.merged_prs`:

| Dimension | Bands |
| --- | --- |
| `diff_size` | S: under 50 additions plus deletions; M: 50 through 299; L: 300 or more. |
| `changed_files` | S: zero or one; M: two through five; L: six or more. |
| `labels` | The sorted distinct JSON label set from the receipt-linked `swarm.work_item.labels`; `[]` is an observed empty set. |
| `turn_count` | S: under 10 observed turns; M: 10 through 49; L: 50 or more. Priced and unpriced turns both count. |

Every unmerged task remains in `unknown` for every dimension. Missing metadata
also uses `unknown`. Work-item labels are accepted only when both creation and
the latest update predate `as_of`; missing links, malformed arrays, and newer
snapshots are unknown. Intake policy labels are eligibility configuration and
are not issue labels. No GitHub collector is added. Compare initiating models
within a task class and an observed difficulty band.

## Rate denominators

Rates are fractions from zero to one. Every rate has a numerator, denominator,
and unknown-count column. Zero denominators return SQL `NULL`.

- **Success:** positives divided by judged task outcomes (`positive`,
  `reverted`, `ci_failed`, `failed`). Pending maturity, unknown, and censored
  tasks form the unknown count.
- **Attempt success:** succeeded runs divided by terminal runs (`succeeded`,
  `failed`, `escalated`, `cancelled`). Other or post-cutoff outcomes are unknown.
- **First-pass CI:** numerator and denominator are zero, rate is null, and
  every task is unknown. Stored run artifacts and first-pass review verdicts
  do not contain the complete check history of the first implementation head.
  Final `merge_ci` success and merge-queue ejection counts cannot establish
  first-pass CI. Manual follow-up uses a read-only GitHub check-history join
  on the first implementation head, including checks before corrections and
  subsequent heads, with missing or incomplete history left unknown.
- **Fix-up rounds:** count distinct `correct_*` node keys per task across all
  attempts. Report their total, task-count denominator, zero unknown count,
  and mean. Retrying one correction node adds attempts, not another round.
- **Escalation:** a task is observed escalated if any pre-cutoff run has
  status `escalated`, any pre-cutoff run pin carries a nonempty
  `escalated_from` pool-escalation marker, or the
  receipt has nonempty valid `escalation_json` with `updated_at < as_of`.
  Quota-driven reviewer substitution and planner-chosen per-node models carry
  no marker and do not count. This measures
  observed escalations, without assigning model strength.
  Divide escalated tasks by escalated tasks plus tasks with observable absence.
  Absence is unknown when run model evidence is missing or the receipt's latest
  update is at or after `as_of`; a positive signal still counts as observed.
- **Revert:** reverted merged tasks divided by merged tasks with a judged
  revert window (reverted or matching mature clean-window evidence). Other
  merged tasks form the unknown count. Unmerged tasks are outside this rate.

## Limits and outstanding acceptance

The `outcomes_report_test` BDD target executes all four sections from the SQL
file against PostgreSQL 16 with chart migrations and SAVEPOINT-isolated fixtures.
It checks mature and unjudged outcomes, strict cutoffs, retries and model switches,
duplicate ownership paths, cost bounds, terminal elapsed times, difficulty bands,
and unknown first-pass CI. The loader rolls back the report's read-only SET
before returning the fixture session. These fixtures cover repository predicates;
the operational checklist below still requires sampled production evidence.

Starts, receipts, runs, and work items contain mutable fields. The report
cannot reconstruct every past status, session binding, or label set. It masks
post-cutoff finishes, retains reservation exposure for post-cutoff start
updates, and leaves newer receipt escalation and label evidence unknown.
Merged-PR metadata and historical links can arrive after the requested time;
they identify earlier merges by their stored merge timestamp, without proving
that the report could have known about them then. Each SELECT has its own
read snapshot; a live report can change between sections.

Revert detection reuses the existing seven-day scanner. Rebase merges expose
only the last rebased commit as `merge_commit_sha`. A partial revert of an
earlier commit without a PR reference can be missed. Bounded or failed scans
cannot establish a clean window; delayed observations can leave mature tasks
pending. Exact-head CI, revert samples, unknown usage, reservation costs, and
query execution cost still need the read-only operational checklist on #6716.

Low success rates, correction rounds, escalations, and high cost or elapsed
time per positive can flag underpowered routing. The report cannot show
overpowered routing: whether a cheaper model would also have succeeded is a
counterfactual. Model-bench (#6702) covers that comparison on shared tasks.
#6700 is later rating work. This change makes no rating or routing decisions.
Policy generations and model availability still confound comparisons; choose
cohorts that match the policy generations being compared.
