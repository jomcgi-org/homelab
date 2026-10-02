# Clarity calibration set for the KG audit loop (#6721)

Forty live knowledge-graph notes, sampled uniformly at random and hand-labelled
for clarity (five pass/fail criteria plus a 1 to 5 score), correctness against
the checkout and the hub, and a defect cause. The labels live in
`clarity-calibration.jsonl`, one object per note. The set exists to calibrate
the audit judge (Astra, with an Opus slice), so borderline cases are labelled
as borderline rather than rounded to a tidy score.

## Sampling

Run at 2026-10-02T06:56:02Z against the CNPG primary `monolith-pg-1` on the
GKE hub (`kubectl exec ... psql`, read-only). The population was 7,916 notes.

```sql
SELECT json_build_object(
  'id', n.id, 'note_id', n.note_id, 'title', n.title, 'content', n.content,
  'type', n.type, 'scope', n.scope, 'verification_state', n.verification_state,
  'confidence', n.confidence, 'valid_from', n.valid_from, 'valid_until', n.valid_until,
  'observed_at', n.observed_at, 'tags', n.tags,
  'sources', (SELECT array_agg(DISTINCT r.source) FROM knowledge.atom_raw_provenance p
              JOIN knowledge.raw_inputs r ON r.id = p.raw_fk WHERE p.atom_fk = n.id)
)
FROM knowledge.notes n
WHERE n.deleted_at IS NULL
  AND n.type IN ('fact','atom')
  AND n.verification_state <> 'legacy'
  AND (n.scope IS NULL OR n.scope NOT LIKE 'personal:%')
ORDER BY random()
LIMIT 40;
```

Population shape at sampling time: 3,754 verified, 4,093 unverified, 68
invalidated, 1 disputed; 7,811 of 7,916 are `repo:jomcgi-org/homelab`. All 40
sampled notes are `repo:jomcgi-org/homelab` and `type = fact`. Raw sources in
the sample: codex-session 17, ember-session 10, claude-session 7, agent-report
6. Personal scopes were excluded in the query and the output files were
re-checked by hand before commit.

## Rubric

Each criterion is pass/fail on the note as a judge would see it: title,
content, and the metadata columns above. `observed_at` does not count as an
anchor because every note has one and it says only when extraction ran.

| Criterion | Pass when |
|---|---|
| self_contained | The note is understandable without the session that produced it. Fails on "the report", "the incident", "the chart", "Turn 1 result", "Tool output [63]", receipt numbers, or SHAs with no named branch. |
| scoped | The note names the system or component (and the environment, when the claim is environment-specific) so a reader knows where it applies. Fails on "the environment", or when the note is filed under a scope that does not match its subject. |
| temporally_anchored | A date, commit, version, or `valid_from` says when the claim was true. Timeless library facts pass. Fails when a time-sensitive word ("remains", "currently", "does not") has nothing to anchor it. |
| evidence_cited | The evidence can be re-read by someone else: a path (with or without lines), commit, issue, PR, or a command whose output is reproducible. Tool output, test counts, raw session results and DB rows nobody can re-query fail. |
| one_claim_unambiguous | One claim, no hedging soup, and no generalisation of a single run into a rule about what the system "requires" or "does". |

Score: 5 = all five pass and the note is crisp; 4 = one fail, or all pass with
a defect such as a dead path; 3 = two fails; 2 = three fails; 1 = four or
more. Judgement overrides the arithmetic where noted in the rationale.

### Refinement after labelling: add a `durable` flag

The sample split along an axis the five criteria do not measure. Eleven notes
(28 percent) record a single event in one session, such as a tool call that
failed on a laptop permission, one `ci test` exit code, one patch retry, or
one planner decision. Several of these score 4 on clarity because they are
precise, dated and cite something, yet they are not knowledge about the
system and nothing could ever verify or supersede them. The rubric would
reward them while an auditor could never resolve them.

So each row also carries `durable` (true when the claim outlives the session
that produced it). It is not folded into the clarity score, because mixing
the two would hide the signal: of the 11 non-durable notes, 8 are
unverifiable and none is correct; of the 29 durable notes, 23 are correct.
The judge should score clarity and durability separately, and the audit
should treat non-durable notes as a placement defect (they belong in session
logs, not the KG) rather than a clarity one.

Two other sharpenings came out of the sample. `evidence_cited` must demand
re-readable evidence, not any evidence: "assertion output" and "list-routine-jobs
output showed" read as citations but cannot be checked. And `scoped` must
include the stored `scope` column, because one well-written note about the
freetoken fork is filed under this repo.

## Results

### Criterion pass rates (n = 40)

| Criterion | Pass | Rate |
|---|---|---|
| self_contained | 29 | 72% |
| scoped | 36 | 90% |
| temporally_anchored | 27 | 68% |
| evidence_cited | 23 | 58% |
| one_claim_unambiguous | 33 | 82% |
| durable (extra flag) | 29 | 72% |

### Clarity score distribution

| Score | 1 | 2 | 3 | 4 | 5 |
|---|---|---|---|---|---|
| Notes | 3 | 5 | 6 | 15 | 11 |

Mean 3.65. 26 of 40 score 4 or 5.

### Correctness

| Label | Count | Mean stored confidence |
|---|---|---|
| correct | 23 | 0.973 |
| partially correct | 3 | 0.957 |
| wrong | 5 | 0.942 |
| unverifiable | 9 | 0.986 |

Checked against the worktree at `b4623335a` (main, 2026-10-02), `gh` for
issue and PR state, and read-only `kubectl` on the hub for the two
environment claims. Stored `verification_state` against the hand label:
verified 18 correct, 5 unverifiable, 2 wrong; unverified 5 correct, 3
partially correct, 4 unverifiable, 2 wrong; invalidated 1 wrong (it is still
in the sampling frame).

By raw source: all 9 unverifiable notes came from `codex-session` raws
(17 sampled, so 53 percent of that source). `agent-report` was 6 of 6
correct. `ember-session` was 6 correct, 2 partially correct, 2 wrong.

### Cause tags (25 defective notes)

| Cause | Count |
|---|---|
| other: ephemeral session event | 9 |
| stale after code change | 4 |
| other: verifier meta-commentary | 3 |
| extraction over-generalised | 2 |
| duplicate not merged | 2 |
| other: unmerged change recorded as fact | 2 |
| missing supersession | 1 |
| source wrong | 1 |
| other: wrong scope | 1 |

Three `other` tags recurred enough to propose for the taxonomy: **ephemeral
session event** (the raw was a delivery log, not a finding), **verifier
meta-commentary** (an agent-report verifier's running commentary, "the report
claims ... not independently confirmed", was stored as the fact), and
**unmerged change recorded as fact** (a session's working tree or a closed PR
was described as the repo's behaviour). The last one is the most dangerous of
the three because the notes it produces are clear, cited and wrong.

## Five instructive examples

1. **2188564, "Codex and Pi interrupt adapters do not signal their child
   processes".** Clarity 5, wrong. Dated 2026-08-04, cites `shim.py:1157` and
   issue #4321. All four `interrupt()` implementations now signal their
   process and #4321 is closed; the note has no `valid_until`. A judge that
   scores clarity alone would rank this near the top of the graph. Cause:
   missing supersession.

2. **2188970, "Owner-preserving reconciliation retains workloads after
   assignment errors".** Clarity 5, wrong. Cites "Review of PR 6069". PR 6069
   is closed unmerged and `git log -S` finds no commit that ever contained
   `:owned_with_assignment_error`. The extractor turned a review of proposed
   code into a fact about the repo. Cause: unmerged change recorded as fact.
   2187854 (deleted files map to the nearest Bazel package) is the same
   failure from a working tree: `affected-targets.sh` still falls back to
   `//...` on any deletion.

3. **2186535, "Token broker network policy admits node sidecars on port
   8080".** Clarity 4, wrong since 2026-09-22 when `ff457206e` deleted
   `tokenbroker-networkpolicy.yaml` as inert. True when written, dated, and
   nothing closed it. This is the case the audit loop is for: a repo diff
   that should have produced a supersession and did not.

4. **2185512, "The repository commit could not create its worktree lock".**
   Clarity 2, unverifiable, not durable. One sandbox permission error on a
   laptop, cited as "Tool output [63]". It is precise and it is not
   knowledge. Nine notes in the sample are of this kind and all nine came
   from codex-session raws.

5. **2191842, "The AgentTurn model at commit 8902bdc13 stored reported and
   list cost in separate fields".** Clarity 3, correct. Every claim checks
   out, including #5892 being closed, but the content is a verifier's
   commentary ("The report claims ... that issue-state claim was not
   independently confirmed") rather than a statement of the fact. The
   agent-report lens is storing its own hedges.

Borderline rows worth a second opinion from Joe: 2187310 (node pools, correct
and dated but "the incident" is never named; scored 4), 2184659 (SQLAlchemy
lazy loading, correct and clear but not about this repo; placement rather
than clarity), 2189203 (over-budget reviews, half verifiable in code and half
resting on four DB rows), and 2192042 (PR #6357 review, correct but three
hedged clauses).

## What was surprising about the KG overall

- **Stored confidence carries no signal.** Wrong notes average 0.94,
  unverifiable notes 0.99, correct notes 0.97. The extractor is confident
  about everything, so confidence cannot be used to weight the audit sample.
- **`verified` is not much safer than `unverified`.** 2 of 25 verified notes
  are wrong and 5 are unverifiable; the verified label was earned once and
  has never been re-earned, which is the decay #6721 is about.
- **The defect profile is placement and staleness, not prose.** Only 9 of 40
  fail more than two clarity criteria, and the ones that do are mostly
  session receipts that should never have become notes. The wrong notes are
  the clear ones. A judge calibrated on this set should expect to flag
  roughly a quarter of notes as not-knowledge and a tenth as stale, and
  should not expect clarity to predict correctness.
- **Dead evidence paths are common and harmless to correctness.** Four notes
  cite paths that moved (graph -> swarm -> factory, agent_sessions ->
  factory/execution) and every one of them is still right. Line numbers had
  drifted in six more. A path-resolution step before judging would stop the
  audit from mis-filing renames as defects.
- **Duplicates are exact.** 2188358 and 2189395 say the same thing three
  days apart from different sessions, and the second note_id carries a `-2`
  suffix, so the writer knew. Near-duplicate detection at write time would
  have caught it.
- **Invalidated notes stay in the frame.** 2190935 was invalidated two
  minutes after it was written and still answers `deleted_at IS NULL AND
  verification_state <> 'legacy'`. The audit's uniform stream should exclude
  `invalidated` or it will spend budget re-judging settled notes.
