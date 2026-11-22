# Scheduled agents

Proposal for [#7935](https://github.com/jomcgi-org/homelab/issues/7935),
researched on 2026-11-14 at
[`1f536d949`](https://github.com/jomcgi-org/homelab/tree/1f536d94934ccfef73aa3a2129f00be60f8a44ca).
Build delivery health, trust drift and documentation drift first. Add the
other jobs only after their inputs exist and the first three earn their cost.
This document creates no agents, schedules or implementation issues. Joe
chooses which to build.

## What the evidence supports

The research covered the root guidance, every scoped `AGENTS.md` and
`REVIEW.md`, the architecture files, first-parent history, open defect
reports, closed PRs, review submissions and reachable CI logs. Counts use
committer dates from 2026-09-16 00:00 UTC through the pinned November 14
head, a 60-calendar-day window. A fix subject is a churn signal, not proof
of an incident. Paths overlap; do not add their counts.

| Area | Commits touching path | `fix` subjects | Explicit `revert` subjects |
| --- | ---: | ---: | ---: |
| Repository | 4,036 | 1,405 | 6 |
| Retiring `projects/monolith/factory` | 839 | 507 | 1 |
| New `projects/factory` | 136 | 49 | 1 |
| `projects/embervm` | 824 | 280 | 3 |
| `projects/monolith/frontend` | 459 | 172 | 1 |
| `projects/monolith/grimoire` | 140 | 44 | 0 |
| `bazel`, `.github`, `buildbuddy.yaml` union | 258 | 122 | 0 |

Reproduce the rows with the following history, selecting commits that touch
each path, then counting subjects matching `^fix(?:\([^)]*\))?!?:` and
`^revert(?:\([^)]*\))?!?:|^Revert ` (case-insensitive for reverts).
The denominator includes 916 chart publication and 308
autoformat commits; it is not 4,036 independent engineering changes.

```sh
git log 1f536d94934ccfef73aa3a2129f00be60f8a44ca \
  --first-parent --since-as-filter=2026-09-16T00:00:00Z \
  --format='%H%x09%cI%x09%s' --name-only
```

These examples determine the priorities:

| Evidence | Consequence for this proposal |
| --- | --- |
| [#7845](https://github.com/jomcgi-org/homelab/pull/7845), [#7846](https://github.com/jomcgi-org/homelab/pull/7846), [#7850](https://github.com/jomcgi-org/homelab/pull/7850) | Chart publication blocked the shared write-back. Two sampled [BuildBuddy](https://jomcgi.buildbuddy.io/invocation/cf86b934-259a-4e38-901d-e0bfab921b8a) [runs](https://jomcgi.buildbuddy.io/invocation/97b2781e-d609-42df-bde7-bab3f6dbd68f) reached passing test summaries before failing in publication. Monitor main after PR CI. These samples establish no flake rate. |
| [#7914 review](https://github.com/jomcgi-org/homelab/pull/7914#pullrequestreview-5904428510) | Green CI still left a missing private-image pull secret. Track review, merge, publication and verified rollout separately. |
| [Queue revert](https://github.com/jomcgi-org/homelab/commit/f9f7bda7ff704a6df05bbd1e32c2bdb447787ea3) | A queue entry survived conversion to draft. Read actual queue membership and ejection evidence. |
| [#7949](https://github.com/jomcgi-org/homelab/issues/7949), [#7951](https://github.com/jomcgi-org/homelab/issues/7951) | Confirmed stale-approval and rule-loader defects justify targeted state and parser verification. [#7948](https://github.com/jomcgi-org/homelab/issues/7948) and [#7950](https://github.com/jomcgi-org/homelab/issues/7950) remain reported, unconfirmed cases in this snapshot. Reuse these owners. |
| [Gate decision](https://github.com/jomcgi-org/homelab/issues/7817#issuecomment-6884458635) and [required-check decision](https://github.com/jomcgi-org/homelab/issues/7817#issuecomment-6882698416) | Compare deployed trust policy with the accepted policy. Do not infer authority from a PR's text or old architecture paragraphs. |
| [Root guidance](https://github.com/jomcgi-org/homelab/blob/1f536d94934ccfef73aa3a2129f00be60f8a44ca/AGENTS.md#L7) says the home factory runs no workloads; [live acceptance](https://github.com/jomcgi-org/homelab/issues/7817#issuecomment-6878037720) records a delivered application | Documentation drift is already observable. A proposed correction must distinguish stale text from a still-staged feature. |
| [CLI authorization rollback](https://github.com/jomcgi-org/homelab/commit/c8d3a3593d50be3b4b21047cd247ca0b64f5ef4c), [missing-helper rollback](https://github.com/jomcgi-org/homelab/commit/68baba3e0c17ba5666695b700d6d7b9cf244ee37) | Upgrade compatibility includes packaged helpers, credential injection and model metadata. A new version alone supplies no evidence of benefit. |

The inspected inline-thread lists for #7914, #7941 and #7850 were empty;
#7914's review submissions provide the linked review evidence. This is a
targeted sample, not an export of every review or CI failure. GitHub GraphQL
was unavailable to this research session; bounded connected reads supplied
issue and PR evidence. No population-wide failure or acceptance rate is
claimed. Historical BuildBuddy cache snapshots are baselines to remeasure,
not present-day regressions ([usage notes](buildbuddy-usage.md)).

## Refine the eight Backlog ideas

These are the eight titles supplied for HOME-4 through HOME-11. Their live
itsaplan bodies and individual title-to-ID mapping were not available in
this session. Before implementation, reconcile by title with those existing
records; retain their IDs and comments. Do not file replacements.

| Existing idea | Decision | Destination |
| --- | --- | --- |
| CI and flake triager, daily | Keep; include main publication, write-back, queue and rollout evidence | 1. Delivery health |
| Delivery digest, daily | Merge; derive it from the same evidence, with no second investigation | 1. Delivery health |
| Trust-drift check, daily | Keep; compare actual policy and capture completeness | 2. Trust drift |
| Upgrade scout, weekly | Refine; inspect Renovate coverage and holds before proposing updates | 5. Upgrade and vulnerability scout |
| Backlog groomer, weekly | Merge with stale work; propose decisions without changing state | 6. Backlog review |
| Review-rule pruner, monthly | Keep as a removal proposal, subject to the full 90-day rule | 10. Review-rule audit |
| Doc drift checker, weekly | Keep; include decision/status links and changed prose | 3. Documentation drift |
| Stale work janitor, weekly | Reject autonomous cleanup; merge read-only diagnosis | 6. Backlog review |

## Use the planner contract

Every model job uses a separate `homelab-planner` identity and fresh,
repository-bound session. It gets read-only repository access, no push, no
coding-agent key, no itsaplan tools and no column moves. It returns only
`{title, body, evidence links, fingerprint, priority}` proposals. A body
never lists an approval or the human gate as an acceptance check; the
human-gate hold covers them ([decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-759#comment-10159)). The
connector validates each one and files it at or below its schedule's
ceiling: delegated fix, Joe task or Backlog
([decision](https://github.com/jomcgi-org/homelab/issues/7935#issuecomment-6891325789)). Identity and
credential scope are enforced outside prompts, as required by
[#7817](https://github.com/jomcgi-org/homelab/issues/7817).

The following are proposed defaults, not deployed settings:

- The connector allows at most four new proposals/day and twelve/week
  across the portfolio. Phase 1 allows six/week. Per-job limits below can
  only lower these. Zero proposals is a valid result.
- Canonical fingerprints are `repo:area:object:failure-class`. The connector
  validates the configured namespace. Do not add a date, run ID, seed or
  wording hash to make the same problem look new. Dedupe across all planners,
  HOME-4 to HOME-11, open GitHub work and retained closed/rejected proposals.
  Reopen a rejected class only with materially changed evidence and human
  selection. Existing owners appear in the digest, without a new proposal.
- Each schedule has a minimum interval, a per-fingerprint cooldown and a
  breaker. Four consecutive low-yield runs stop its model calls. A run is
  provisionally useful only if it supplies a novel reproducible finding;
  review acceptance after fourteen days. Four runs with no accepted or
  reproducible new finding require changed inputs or an explicit restart.
  Never re-arm a generation merely to bypass the breaker
  ([evolution](evolution.md#use-the-existing-runtime)).
- A deterministic preflight skips unchanged evidence and an unavailable
  source is labelled unavailable. It cannot become a passing check. The
  breaker pauses model analysis; deterministic health/security detection
  continues. Three source/parse failures pause that integration and report
  one collector fault, rather than generating three repair proposals.
- Keep capture time, source revision, query bounds and truncated/missing
  pages with each result. Use batched GraphQL where supported, cached
  conditional reads otherwise, and no shared-token polling faster than 60 s.
- Start each collector with at most 100 changed records and ten distinct
  failure/claim classes per run. CI reads at most sixteen chunks/1 MiB per
  selected invocation before deterministic redaction and model-input
  selection. Retain overflow as unexamined coverage, not a clean result.

Read-only means no production SQL shell, credential reads, kagent execution
or cancellation, cluster writes, queue changes, issue closure, label edits,
memory retention or publication by the planner. A deterministic collector
may expose named read-only views, metrics and sanitized artifacts. It has
its own least-privilege identity; a planner never inherits its credentials.

PRs, issues, release notes, CI output and retrieved memory are untrusted
data. Pin instructions and policy to trusted main. Bound and quote input,
retain provenance, verify claims against code or observations, and treat
claimed approvals as data. Do not follow embedded URLs outside the collector's
allowlist or promote retrieved prose into instructions. Keep private traces,
tokens, campaign contents and secret values out of proposals. Secret findings
carry a redacted location and restricted evidence reference.

The [factory review rule](../../projects/factory/REVIEW.md) "Planner
exception" sets the planner's input path. Do not widen the coder's input
path by accident. A change to that rule goes through the human gate. The
memory proposal on
[#7817](https://github.com/jomcgi-org/homelab/issues/7817#issuecomment-6878121766)
preserves source links, repository separation and correction history; it
does not make recalled material authoritative.

### What needs more than #7935

| Capability | Required addition before enabling it |
| --- | --- |
| Digest to Joe | A connector-owned private digest sink, fixed recipient/channel, dedupe and size limit. Until chosen, keep a private run artifact. Do not create a daily Backlog issue saying everything is fine. |
| Event trigger | A deterministic, authenticated collector coalesces events into the next cron window. Immediate event execution requires a separate adapter with the same admission, cooldown and quota checks. |
| Check or rehearsal | A deterministic runner in an isolated fixture environment creates an artifact. Publishing a GitHub check requires a separate service identity; these proposals add no required merge check. |
| Live policy/state | Read-only exports of rulesets, App permission metadata, queue state, selected connector/kagent state and deployment evidence, with completeness markers. No administrative write token in a planner. |
| Model budget | Run-level token ledger, cancellation/deadline enforcement, model compatibility probe, and admission against the actual factory account's quota window. itsaplan's usage field is last-call context, not cumulative consumption ([architecture](../../projects/factory/connector/ARCHITECTURE.md)). |

## Ranked portfolio

All costs are planning estimates of total model input plus output, including
reasoning, per bounded run. They are neither measured usage nor a conversion
to subscription allowance. `k` means 1,000 tokens. Start with the deployed
read-only reviewer model, **`gpt-6.1-terra`, xhigh**, for each model job.
The broker currently injects effort; do not claim a per-agent low/medium
setting works without adding and testing that routing. Terra's fallback
catalogue metadata in Codex 0.161.0 also requires bounded-output conformance
([recorded model decision](https://github.com/jomcgi-org/homelab/issues/7817#issuecomment-6881930021)).

| Rank | Job | Cadence | Input + output/run | Weekly estimate | New proposals/run; day | Fingerprint cooldown |
| --- | --- | --- | ---: | ---: | --- | --- |
| 1 | Delivery health and digest | Daily | 24k + 6k | 210k | 2; 2 | 7 days |
| 2 | Trust drift | Daily diff | 20k + 5k | 175k | 1; 1 | 7 days |
| 3 | Documentation drift | Weekly | 28k + 7k | 35k | 2; 2 | 30 days |
| 4 | State and parser audit | Weekly, after harnesses | 35k + 15k | 50k | 2; 2 | 14 days |
| 5 | Upgrade and vulnerability scout | Weekly | 25k + 5k | 30k | 2; 2 | 30 days |
| 6 | Backlog and stale-work review | Weekly | 16k + 4k | 20k | 2; 2 | 30 days |
| 7 | Grimoire rehearsal | Nightly deterministic; at most two novel-failure analyses/week | 16k + 4k on failure | 0–40k | 1; 1 | 7 days |
| 8 | Public-app performance | Nightly probes; weekly changed-result analysis | 12k + 3k | 15k | 1; 1 | 14 days |
| 9 | Migration and observability evidence | Weekly during migration | 20k + 5k | 25k | 1; 1 | 14 days |
| 10 | Review-rule audit | Monthly | 20k + 5k | 6.25k amortized | 1; 1 | 90 days |

The four-run breaker applies to every model job, with job-specific criteria
below. The minimum schedule interval is its listed cadence; event evidence
does not buy extra runs. All jobs emit Backlog proposals only for novel work.
Deterministic results and digests use the separate paths above.

### 1. Find delivery failures and report outcomes

Read current-head statuses/checks, bounded redacted BuildBuddy logs, actual
merge-queue entries/ejections, main publish and write-back receipts, deployed
chart/image revisions and rollout verdicts. Include broker quota age/reset
and cumulative run usage once available. The
[CI guide](ci-triage.md), [delivery mechanics](reference.md#delivery-mechanics)
and the publication failures above supply the evidence model.

Cluster failures by target, normalized exception and stage. A flake needs
different outcomes for the same relevant code/config, not merely a retry
that passed after an edit. Compare build p50/p95, cache hit rates and remote
bytes with the previous fourteen comparable days, stratified by target set,
runner and cold/warm cache. Propose investigation after three comparable
runs exceed a chosen 20% budget; establish the baseline first.

Daily batching supplies enough failures to distinguish repeated causes.
Weekly would leave main publication broken for days; per-failure model calls
would duplicate the connector's existing revision loop. CI failures, queue
ejections and merges mark evidence dirty for the next run. Deterministic
urgent alerts remain independent.

Use fingerprint `delivery:<stage>:<target-or-app>:<cause>`. Read-only checks,
Actions metadata where relevant, BuildBuddy evidence and deployment exports
are sufficient. Output at most two repair proposals plus one private digest
of merged, deployed, failed and waiting work, capped at ten linked items.
Measure accepted proposals/novel proposals and time from failed publication
to acknowledged owner. Keep it after four weeks if at least half its
proposals are accepted and it has found an unattended delivery failure.
Drop model narration if a deterministic digest provides the same decisions.
Risks are log injection, calling real bugs flakes, duplicate revision work
and mistaking a successful merge for deployment.

### 2. Detect changes to trust

Compare actual ruleset 9180009, required check identities, ALLGREEN, bypass
actors, installed App permissions and repository scope with an approved
baseline. Include CODEOWNERS, trusted reviewer/approver lists and human-gate
paths/labels from main and rendered live configuration. Compare Cilium
default-deny and Envoy/Authentik route policy on the factory cluster with
their accepted manifests; keep GKE's separate policy model distinct
([platform rules](../../projects/platform-factory/REVIEW.md),
[factory boundary](../../projects/factory/ARCHITECTURE.md#merge-authority)).

The job reads permission metadata, never App private keys or Secret values.
An operator-owned collector may need administrative **read** access to list
installations/rulesets that the planner token cannot see. Missing pages are
an incomplete audit. A new App install or policy merge marks the next daily
diff dirty. Daily covers out-of-band changes; hourly model rescans add little
when the snapshot hash is unchanged.

Fingerprint `trust:<object-id>:<field>:<unexpected-value-class>`; output one
proposal with before/after evidence. A deterministic high-risk drift alert
must reach the chosen private sink even when model calls are paused. Success
is complete inventories and detection of seeded benign drift in the next
collection, with no permission expansion by the job. Stop the model after
four empty diffs or if false positives exceed half of ten reviewed reports;
retain the collector. Risks are incomplete visibility, exposing permission
metadata, treating accepted staged changes as drift and duplicating the
state audit. Trust drift owns configuration; job 4 owns temporal invariants.

### 3. Reconcile documentation with code and observations

Inspect changed README/ARCHITECTURE claims and their linked code, flags,
deployment receipts and open decisions. Rotate through one unchanged domain
per week. Check prose changed since the last run against
[writing.md](../writing.md); deterministic banned-word/link checks belong
in CI, and subjective copy edits alone do not merit a proposal. Current
architecture mixes old enqueue claims with the newer
[check-only reviewer decision](https://github.com/jomcgi-org/homelab/issues/7817#issuecomment-6882698416).
Quote both the claim and its contradicting source before proposing a fix.
For example, [Grimoire's historical-visibility prose](https://github.com/jomcgi-org/homelab/blob/1f536d94934ccfef73aa3a2129f00be60f8a44ca/projects/monolith/grimoire/architecture.md#L414)
disagrees with the [scope-change implementation](https://github.com/jomcgi-org/homelab/blob/1f536d94934ccfef73aa3a2129f00be60f8a44ca/projects/monolith/grimoire/router.py#L2215),
which retracts history and writes a replacement projection.

Weekly allows related code and documentation to settle. A merge touching an
architecture file or public contract marks the input dirty; it does not
start another model. Read-only Git and issue/deployment evidence suffice.
Fingerprint `docs:<path>:<claim-or-symbol>`; at most two proposals, each
grouping one cause across affected files. Include obsolete decision/status
links. The repo retired ADRs in
[#4667](https://github.com/jomcgi-org/homelab/issues/4667); maintain the
ARCHITECTURE decision history and Direction rows instead of rebuilding an
ADR registry ([reference](reference.md#where-to-look-next)).

Retain if at least half of reviewed proposals correct an operationally
misleading claim within four runs. Stop after four without such a result.
Risks are stylistic churn, interpreting a disabled feature as deleted and
using stale docs as stronger evidence than code/live acceptance. Proposed
authority text must link Joe's decision and receives the existing human gate.

### 4. Check state machines and hostile parsers

Use one weekly analyst for new counterexamples and risk-weighted coverage
gaps. It reads pinned main, test/model inventory, minimized failing corpora
and sanitized state snapshots. It gets no production execution tools.
The connector and reviewer defects above justify the initial scope.
Existing regression tests already cover many individual failures; extend
their sequences rather than proposing a rewrite
([dispatch tests](../../projects/factory/connector/dispatch/engine_test.go),
[review tests](../../projects/factory/reviewer/review)).

| Surface | Invariant to check | Technique and schedule value |
| --- | --- | --- |
| Connector work item, run and attempt | At most one non-quiescent writer across sessions, redelivery and epochs. Every send has a durable attempt; retransmission preserves ID and bytes. A timed-out response or expired reservation does not prove an accepted task stopped. | TLA+/TLC with two epochs, two runs and two sessions, lost/delayed replies, restart and reassignment. Replay counterexamples in Go state-machine/property tests using the fake clock and Postgres fixtures. Small exhaustive bounds in CI; larger bounds weekly. |
| Stop and completion | Lost writer ownership permits no new side effect. Cancellation cannot send work; replacement requires proven cessation. Done requires observed merge. Stale attempts cannot advance current work. | Generated event sequences across guard/write/call/acknowledgement boundaries. Weekly read-only join of connector state and kagent inventories detects stopped-but-active work and multiple live generations. |
| Reviewer verdict and human gate | Success needs a usable report, no retained blocking finding, and required approval on the exact head. Dismissal, a later hold or a superseding review invalidates stale approval. Unknown severity/parse failure never passes. | Truth tables and property tests for report/gate logic; state model for head, labels, approvals and restart. Weekly comparison of live records catches changed external state beyond CI's fixtures. |
| ALLGREEN merge groups | Every queued prefix member remains eligible. A held earlier PR cannot ride a later passing group. An incomplete reread after a hold cannot reuse success. | Three-PR model with prefix groups, ejection/rebuild and API errors; a negative configuration without ALLGREEN must fail. Trust drift checks the actual ruleset daily. |
| REVIEW Includes | Root, ancestors, both rename paths and include closure are covered at the pinned base revision. Heading/blank lines cannot silently omit includes; cycles, missing files and bounds are explicit errors. | Go fuzzing plus properties over an in-memory tree, seeded by #7951 and actual rule files. Short corpus in CI; longer weekly exploration only after a harness exists. |
| Strict config | Unknown/duplicate keys, extra documents, invalid durations, overlapping identities and excessive permissions cannot become permissive defaults. Explicit empty lists keep their meaning. | Fuzz connector/reviewer/broker parsing and mutate valid fixtures. Require semantic reject/accept properties as well as no panic. |
| GraphQL and A2A | Partial data with errors, missing objects, wrong IDs, unknown task states, truncation and incomplete pagination cannot grant merge permission or prove cessation. | Fuzz HTTP fake-server envelopes and call-site properties; retain minimized failures in CI. Weekly longer runs use new protocol versions and corpus seeds. |

Reuse [TLC infrastructure](../../bazel/tla/tlc_test.sh) and
[EmberVM specs](../../projects/embervm/specs/BUILD). Extend the current
EmberVM-only visibility deliberately. [oom-inference's verification](../../projects/oom-inference/specs/README.md)
already separates small CI models and larger runs with broken mutants.
Choose TLA+ first; a Quint migration needs a demonstrated modelling or
maintenance benefit. A deliberately broken writer, stale approval, omitted
include and partial-success response must each fail the intended check.

CI owns small models, known regression seeds and bounded property tests on
every affected change. Scheduled deterministic runners own larger state
spaces and fuzz budgets. Give each harness a proposed 20-minute/2-GiB limit;
resource exhaustion is incomplete, never a pass. A weekly model run happens
only for a novel failing case or a changed risky surface lacking coverage.
Production snapshots can detect a violation; they cannot prove a temporal
property. Missing traces or inconsistent capture epochs are inconclusive.

Fingerprint `invariant:<subsystem>:<invariant-id>:<cause>`; two proposals
with replayable seeds or an exact coverage gap. Success requires a reproducer
and an accepted CI regression or model invariant. Stop the analyst after
four runs with neither; retain CI. Prerequisites beyond #7935 are the
models, fuzz/property harnesses, redacted state collector and isolated
runner. Risks are abstract proofs mistaken for implementation proofs,
incomplete snapshots, untrusted corpus text and duplication of known issues.

### 5. Find upgrade and vulnerability work Renovate misses

Read lockfiles, image/chart pins, Renovate configuration, existing update
PRs, upstream release/security advisories and available scanner/SBOM
artifacts. [Renovate configuration](../../renovate.json) already has Codex
CLI extraction and Helm coverage; Envoy has compatibility holds. The
[factory Renovate deployment](../../projects/platform/renovate/README.md) must be
checked for activation. Do not equate configured coverage with running bots.

Check kagent and its Substrate fork together, the packaged Codex harness
and model catalogue, itsaplan migrations, Envoy/agentgateway interfaces and
CNPG backup-plugin compatibility. For each, state the manager/hold, current
pin, candidate version, affected call site and required conformance test.
The factory [architecture findings](../../projects/factory/ARCHITECTURE.md#findings-that-shape-the-design)
and runtime rollback examples motivate this scope.

Weekly groups release churn. A release or vulnerability advisory dirties
the next run; urgent scanner alerts use deterministic notification. CVE
triage asks whether the deployed version and reachable feature are affected,
including base images and vendored binaries beyond Dependabot. Secret
scanning and provenance verification remain deterministic on push/publish;
the scout reads missing/failed attestations and scanner evidence. It never
downloads or executes arbitrary release assets or tests a secret.
Existing [chart image verification](../../bazel/helm/verify-published-images.sh)
compares publication digests; it does not establish signed provenance.
Unavailable SBOMs or attestations remain unknown coverage.

Fingerprint `upgrade:<component>:<compatibility-line>` or
`vulnerability:<advisory>:<deployed-component>`; two proposals, read-only
registry/advisory access, no credential bodies. Deduplicate scanner/update
owners. Keep if one of four runs finds an actionable unmanaged compatibility
gap or reachable vulnerability. Stop if four runs merely duplicate bots.
Risks are advisory injection, false reachability, untested catalogue
metadata and reviving deliberately held upgrades.

### 6. Review Backlog and stale work together

Read bounded itsaplan issue/run summaries through the connector, open
GitHub work, holds and PR/deployment state. Identify duplicate goals,
superseded proposals, work waiting for a decision, and merged work whose
acceptance remains outstanding. The
[human-handoff contract](reference.md#human-handoff),
[#7817 migration gates](https://github.com/jomcgi-org/homelab/issues/7817)
and [evolution contract](evolution.md#goals-survive-sessions) forbid
silently converting unfinished observation work into Done.

Weekly gives owners time to act. Start with items unchanged for fourteen
days, or blocked across two weekly snapshots; age is a filter, never
authority to close, cancel or delete. Fingerprint
`backlog:<existing-item>:<decision-class>`. Prefer a private digest linking
the existing owner; propose new work only for a distinct missing prerequisite.
At most two proposals, no itsaplan mutations by the planner.

Measure useful owner decisions, not items closed. Retain if half the
suggestions reviewed over four runs produce an accepted decision; stop
after four with none. Never groom human-owned bootstrap work into agent
intake. Risks are duplicate ownership, resurfacing rejected decisions and
closing work merely because a PR merged.

### 7. Rehearse Grimoire without production writes

Use the [local table and rehearsal](../../projects/monolith/dev/grimoire/README.md)
against pinned main, an isolated database and fixture users. Existing
[CI](../../projects/monolith/dev/grimoire/ci.sh) already rehearses relevant
PRs. The nightly job adds clean-main integration across merged changes and
time-dependent behaviour; it does not replace that gate.
Prior failures include [invitation acceptance returning 500](https://github.com/jomcgi-org/homelab/commit/817d958a6d68a1a6f80199b3fcb0ce493fe8c7e2)
and [feed position being lost](https://github.com/jomcgi-org/homelab/commit/1019feaefa0578db8a03888bca6c7f8b26a1bcbc).

Cover DM/player onboarding and join links, audience changes, journal/feed,
notes, inventory, handouts, reload/reconnect and simultaneous clients as
defined in [Grimoire's contract](../../projects/monolith/grimoire/architecture.md).
Record exact build, seed, failed step and sanitized screenshots. No real
campaign records, live invitations or production mutations. A separate
authenticated fixture deployment would require an explicitly isolated
environment and synthetic accounts before any live-boundary rehearsal.
The local harness uses `create_all`; it does not prove production migrations,
OIDC or CDN behaviour. Migration-backed startup needs its own fixture runner.

Nightly deterministic execution has zero model tokens on success. Analyze
at most two novel failures/week, with fingerprint
`grimoire:<journey>:<assertion>:<cause>`, one proposal each. Use a fixture-only
runner identity; the planner reads artifacts only. Keep after fourteen
nights if it exercises a boundary beyond current PR CI and exposes a useful
regression or demonstrably catches a seeded fault. Otherwise retain CI and
drop the redundant nightly job. Four non-actionable analyses pause its model.
Risks are flaky browsers, shared fixture state and leaking campaign data.

### 8. Measure public-app performance

Use anonymous, read-only probes of hikes, ships, stars, trips and scotland,
with fixed device/network profiles and cold/warm cache samples. Track
server-rendered content, data freshness, first meaningful map/content,
interaction latency and transferred bytes. Compare with the public tier's
[cache and data boundary](../../projects/monolith/frontend/src/hooks.server.js)
and [operating checklist](../runbooks/public-tier-checklist.md).
The [cache implementation](../../projects/monolith/frontend/src/lib/cache-headers.js)
records stale ships/hikes payloads and trip edits hidden by a 24-hour cache.

Proposed initial budgets are p75 LCP at most 2.5 s and a 20% regression
against a fourteen-day comparable baseline, with three confirming samples.
These are proposed targets, not measured present performance. Test meaningful
content as well as HTTP 200. Nightly probes observe cache expiry and upstream
changes; weekly analysis avoids opening work for one slow response.

Fingerprint `performance:<route>:<metric>:<cause>`; one proposal. Planner
reads measurement artifacts, with no private routes or query contents.
Retain after four weeks if it finds an accepted regression and probe noise
stays below one false proposal/month; otherwise use deterministic budgets
alone. Risks are geographic variance, cache warming and duplicating ongoing
app work. A default-off app is not an outage.

### 9. Audit migration and observability evidence

During [#7817's migration](https://github.com/jomcgi-org/homelab/issues/7817),
read backup/restore receipts, retention/location metadata, identity recovery
plans, deployment verdicts, Honeycomb trigger/SLO definitions, Prometheus
target health and telemetry redaction tests. No database contents or
unrestricted logs. The factory's [restore runbook](../../projects/factory-cluster/README.md)
and [observability contract](../observability.md) define what to inspect.

Ask whether each moved workload has tested restore and rollback evidence,
whether Garage and its backups share a failure domain, whether restored
ServiceAccount identity can access sessions, and whether missing telemetry
can masquerade as healthy. Check warning/log sampling and route latency
coverage. Audit the existing Honeycomb trigger allocation and
[#7003](https://github.com/jomcgi-org/homelab/issues/7003) before proposing
new triggers. Known backup gaps keep their existing owners.

Weekly is sufficient for evidence expiry; a migration/release marks the
next run dirty. Mandatory pre-cutover checks and restore drills belong to
the migration workflow, not a weekly model's assurance. Fingerprint
`operations:<workload>:<missing-evidence-class>`; one proposal from a
read-only metadata snapshot. Keep if it discovers an accepted acceptance
gap in four runs. Retire migration sampling after cutover and two successful
restore exercises; retain only an observability audit with demonstrated
yield. Risks are reporting stale exports as live, treating replication as
backup, and implying that a model has performed a restore.

### 10. Propose review-rule removal only with evidence

Read rule IDs, current rule text, review findings by ID, their source
failures and CI test coverage over ninety days. The
[rule lifecycle](review.md#changing-rules) requires both no findings in
ninety days **and** a test covering the source failure. Missing review
history is unknown. A rule with no recent findings can still be essential.

Monthly matches that window; weekly has little new evidence. Fingerprint
`review-rule:<path>:<rule-id>`; one proposal with counts, coverage and a
counterexample the replacing test still permits. Read-only Git/check-history
access only. It never edits REVIEW.md or its own policy. Each actual removal
uses the human gate. The recently added rules cannot supply ninety days of
history yet, so start by collecting counts, with no monthly model until a
candidate is eligible. Stop after four eligible runs without a supported
removal; deterministic counts remain. Risks are lost IDs, incomplete
history and confusing zero findings with zero value.

## Port the old routines selectively

Read-only routine metadata inspected on November 14 showed the following.
Metadata contains no prompt or yield history; `last_status: ok` does not
establish a useful outcome. A null next-run time alone proves no terminal completion
([routine API](../../projects/monolith/agent/mcp.py)).

| Old routine | Observed shape | Decision |
| --- | --- | --- |
| `factory-retro-daily` | 3,600-second interval despite its name; latest run November 14 | Port the [baseline/72-hour verdict](evolution.md#improvements-earn-their-place) and source-linked lessons into delivery health. Drop hourly model analysis and automatic refill. Preserve old receipts until retirement. |
| `daily-repo-pulse` | 86,400-second interval; latest run November 14 | Merge into the delivery digest. Keep links and outcomes; drop another daily repository scan. |
| `adr-index-*`, `adr-status-*` | One-shot rows with August last-run timestamps | Drop recurrence. ADR retirement #4667 changed the destination; job 3 checks current architecture decisions and status links. Do not restart completed or held rows. |
| `kg-audit` | 86,400-second interval; last recorded run October 30, next due October 31 | Do not claim it is currently running. Preserve bounded sampling, provenance and cooldown ideas from the [KG architecture](../../projects/monolith/ARCHITECTURE.md#4-agents). Defer a new memory audit until the memory integration and poison-resistance contract are accepted; drop autonomous dispute/retain writes from the planner. |

The old [drain queue](drain-queue.md) still describes a daily retro cadence.
That disagreement itself needs evidence-aware documentation review. This
proposal neither stops nor restarts any old routine.

## Spend one weekly window deliberately

The first three jobs estimate **420k tokens/week** if every daily diff invokes
a model. The full portfolio estimates **606.25k/week**, including two
Grimoire failure analyses and one quarter of a monthly rule audit. A week
containing that monthly audit is **625k**. Reserve 20% for one bounded
repair/retry allowance: **750k tokens in that busy week**. Skips should
reduce consumption; do not spend saved tokens on invented work. Record
input, cached input, output/reasoning and retries separately per job.

There is no defensible fixed-token denominator for one Pro weekly window.
[OpenAI's current pricing guidance](https://learn.chatgpt.com/docs/pricing)
states that model, context, reasoning, tools and caching affect subscription
usage; credit prices alone do not determine it. The hub quota endpoint also
does not establish the new factory account's allowance. A percentage claimed
from 606k tokens would be fabricated.

Proposed admission policy: reserve at most **10 percentage points of the
actual factory account's weekly window** for this portfolio during the
pilot, leaving the rest for coding/review and other use. This is a proposed
adjustable capacity allocation, not a standing dollar cap. Measure isolated
pilot runs with fresh before/after quota readings, subtract known overlapping
usage where attributable, and mark the remainder unknown. Estimate weekly
share as the sum of run counts times each job's observed allowance delta.
Do not enable the full portfolio until two reset windows establish its fit.
Stale quota or an unidentified grant defers optional analysis; it never
means free capacity. Preserve [#7817's single-account design](https://github.com/jomcgi-org/homelab/issues/7817)
and [evolution authority](evolution.md#the-factory-owns-its-working-methods).

### Keep analysis outside interactive hours

Proposed schedule uses **Europe/London**, assuming 09:00–23:00 is the
interactive window. Record the timezone explicitly and dedupe by local date
across DST. These times propose admission windows, not reserved capacity.
Coding can run overnight; use one shared low-priority planner slot, and
defer while coding/review capacity is occupied. Never preempt a writer or
delay a required review for a scheduled model call. Skip a missed window
instead of a catch-up burst. Stop optional analysis at 05:00.

| Window | Work | Maximum wall time |
| --- | --- | --- |
| Daily 01:00 | Delivery health | 30 min |
| Daily 01:40 | Trust diff and analysis if changed | 20 min |
| Daily 02:10 | Grimoire fixture rehearsal and public probes on separate bounded runner | 40 min; analysis waits for planner slot |
| Monday 03:00 | Documentation | 35 min |
| Tuesday 03:00 | Upgrades/vulnerabilities | 30 min |
| Wednesday 03:00 | Backlog/stale work | 25 min |
| Thursday 03:00 | State/parser evidence analysis | 60 min |
| Friday 03:00 | Migration/observability | 30 min |
| Saturday 03:00 | Performance analysis | 25 min |
| First Sunday of month 03:00 | Eligible review rules | 30 min |

Longer deterministic model/fuzz checks need their own bounded runner and
must respect the same spare-capacity rule. The private digest can be
delivered at 08:00 without another model call. If deferrals hide a whole day
of coverage, report the missed coverage once; do not falsely report green.

## Roll out only what earns its place

1. **Finish #7935 first.** Prove separate identity, strict proposal parsing,
   evidence requirement, cross-job dedupe, atomic run/day/week accounting,
   cooldown, breaker and no delegation. Replay the same result after a lost
   connector response and prove no duplicate Backlog work. Reconcile the
   eight existing titles before admitting new proposals.
2. **Prepare inputs and authority.** Add the scoped untrusted planner input
   contract, read-only collectors and private evidence retention. Test that
   malicious issue/log/memory instructions cannot change credentials, tools,
   destination or policy. Verify itsaplan Community cron against the pinned
   version: #7935 says it exists, while older architecture prose conflates
   schedules with Pro. Verify the actual integration before enabling it.
3. **Pilot the first three.** Delivery health covers failures after green
   CI; trust drift checks the newly changed merge/identity boundary; docs
   drift prevents agents and operators following superseded state. Run
   deterministic collectors first, then bounded model analysis for two quota
   reset windows. Record accepted, rejected, duplicate and unavailable
   results, token use, quota delta and displaced coding time.
4. **Build verification prerequisites.** Add the small models and parser
   harnesses through separate selected work, with negative cases and CI
   registration. Add fixture runners and performance baselines. Then select
   jobs 4–9 individually from measured gaps. Begin rule-count collection
   now only if separately selected; wait for a full eligible window for job 10.
5. **Review yield.** Keep jobs meeting their metrics, merge overlapping
   outputs and stop model passes whose deterministic reports suffice. Every
   later implementation PR supplies its own baseline, live acceptance and
   rollback under [evolution.md](evolution.md).

### Do not build

- An hourly general improvement scout or a second refill loop. It overlaps
  delivery/backlog evidence and bypasses #7935's human-selected Backlog.
- An autonomous janitor that cancels sessions, deletes branches or prunes
  data. These are ownership decisions. The factory triage job (HOME-386)
  closes or splits issues only through the connector's
  [triage actions](../../projects/factory/connector/triage.md)
  ([decision](https://factory-plan.jomcgi.dev/factory/issue/HOME-386#comment-3915)).
- An ADR index/status scheduler. The repository retired that format.
- A nightly model that repeats passing tests, rewrites prose, enumerates
  CVEs without reachability, or demands more tests by coverage percentage.
  Use deterministic gates and target demonstrated risky gaps.
- A second reviewer, merge enqueuer or gate editor. Required CI, the reviewer
  App and human authority remain where the current contract places them.
- An automatic upgrade stream duplicating Renovate/Dependabot or clearing
  version holds. Propose only uncovered, evidenced work.
- A memory writer that turns issue text or CI output into durable authority,
  or a restore/chaos agent with production write access. Those exceed the
  planner contract and need separate design and acceptance.

### Decisions for Joe

Choose the first three or change their order; choose the private digest and
urgent-drift destination; accept or revise the 10-point pilot allocation and
overnight window. Select any extra live metadata access explicitly, especially
App/ruleset administration reads. After the pilot, choose which additional
jobs justify implementation. This docs PR approves none of those deployments.
