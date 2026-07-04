# Agentic Friction Spec: what makes changing code slow for agents

Status: Complete (evidence gathered 2026-07-03/04; remediation in the companion plan)
Date: 2026-07-03
Author: Claude (session-driven analysis of git history, merged PRs, and 188 Claude Code session transcripts)

## Goal

Changing code in this repo should be fast and easy for agents. This spec inventories
the patterns that measurably slow turns, force repeat PRs/commits, or burn wall time
on avoidable round-trips, each with quantified evidence. A companion plan document
addresses them.

## Evidence base

- Last 300 commits / 100 merged PRs on `main` (git + `gh` analysis).
- 1,149 session transcript files (707 MB, 2026-06-06 to 2026-07-03) scanned for
  friction signatures; 8 recent large sessions deep-mined.
- Deploy pipeline source: `bazel/helm/push.sh.tpl`, `bazel/helm/chart-version.sh`,
  `buildbuddy.yaml`, `.claude/settings.json` hook stack.

## Headline numbers

- Core window: 296 commits / 208 merged PRs across 2026-07-01 to 07-04.
- CI compute is NOT the bottleneck: BuildBuddy "Test" median 46s; PR time-to-merge
  median 3.0 min (p90 31 min). Zero genuine CI check failures in the 30 most
  recent merged PRs. The cost lives in PR count, deploy convergence, and
  session babysitting, not CI wall time.
- 20 of 208 PRs (9.6%) were chart-bump-only PRs: pure process overhead.
- 75 fix-after-feat chains within 24h; ~13 map to named infra gotchas, the rest
  are ordinary iteration.

## Friction patterns

### F1. Chart version / deploy pipeline dance (highest cost)

The single largest recurring cost. Cross-session scan: `targetRevision` appears in
138/188 sessions, `chart-version-bot` in 142/188, `OutOfSync` in 94/188.

Mechanics (from `bazel/helm/push.sh.tpl`):

- On `main`, `helm push` is **unconditional**: every merge re-publishes each chart at
  whatever version `Chart.yaml` holds, with freshly pinned image tags (tags are
  stamped `YYYY.MM.DD.HH.MM.SS-shortsha`, so every repackage differs). A code merge
  without a trailing bump **mutates an already-deployed chart version in place**,
  producing the permanent `sync=OutOfSync` + `operationState=Succeeded` wedge
  (documented 2026-07-04, PRs #3153/#3156/#3165).
- Bump numbers race between concurrent sessions; rebase-merge silently **drops
  duplicate bump commits** (#3175/#3178), so a PR's code can land while its chart
  never re-publishes: the change silently never deploys.
- Result: trailing bump-only PRs are a standing genre: **20 of 208 PRs (9.6%)**
  in the core window touch exactly `chart/Chart.yaml` + `deploy/application.yaml`.
  Root causes from commit bodies: same-version republish race (8),
  chart-version-bot skipped a needed bump (6), cross-chart dependency miss (2),
  unstated (4).

### F2. CI round-trips for locally-catchable failures

- `style: auto-format` commits by ci-format-bot (8 in last 300 commits): each means
  a push landed unformatted, costing a CI round plus rebase churn on the branch.
  Observed drift classes: Python formatting, gazelle BUILD drift, docs-manifest
  regeneration (9dff9812e, 077c5378e, caf1b5c30, 020c4c188).
- Migration-version collisions produced 4 fix commits (8c7553632→c488f32ba
  0.267.0→.1→.2, 5c822a67e, 3283710d5) before a CI guard landed (#3143). The
  edit-time hook checks intra-file timestamp order only; collisions happen against
  the applied DB head and across concurrent PRs.
- "Bump a config value, forget the test asserting on it" remains a documented
  second-push cause.

### F3. Known gotchas still costing repeat PRs

Documented in memory/CLAUDE.md yet still recurring in the last 300 commits:

- `public_reader` grants missing for new public schema: 1 feat needed 2 follow-up
  fix commits (f5cee6160, e5f5155b8 after 26207722f); signature appears in
  81/188 sessions.
- apko lock `config.checksum` follow-up fix (5937526f9) after an apko.yaml edit
  (9/188 sessions hit this).
- ArgoCD diff wedges, 4 fix commits with distinct root causes: duplicate env var
  collapse (b85045e39), emptied pod-template annotations map (two attempts:
  ef7466798 then 30e0f8bf1), app-level ignoreDifferences inert on v3.1.6
  (148eb6864). `OutOfSync` debugging appears in 94/188 sessions; the diagnosis
  recipes live only in one session's memory, not in the repo.
- Same-origin proxy for public reads (2d48e4b69), runfiles miss (9b1c08c17):
  1 fix commit each.

### F4. Waiting on CI/rollout with no leverage

- `gh pr checks` polling appears in 149/188 sessions; `sleep` loops in 112/188.
- CI flakes force manual retriggers: BES sidecar flake (15 sessions, 1 retrigger
  commit 3327d26d5), Docker Hub 429s (4 sessions), "No registered executors"
  (18 sessions).

### F5. Silently dead or noisy guardrails

- Two Write/Edit hooks have had their executable bit missing since 2026-06-13 and
  exit 126 on **every** Edit call, enforcing nothing: `check-large-migration-sql.sh`
  (guards the 256KiB migrations-ConfigMap wedge) and
  `check-lifespan-startup-mock-sync.sh`. In just two deep-mined sessions that is
  256 silent failures (128 Edit calls x 2 hooks, 100% failure rate). Nothing
  tests that configured hooks are executable.
- The superpowers plugin's SessionStart hook fails with exit 127 on every session
  start/compact: the cached 4.3.1 hooks.json single-quotes
  `'${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.cmd'` so the variable never expands.
  (User-level plugin cache, not repo; fix is a plugin update.)
- The Write/Edit hook stack is 28 sequential scripts, ~0.9s per edit. Acceptable
  today, but unbounded growth pattern with no liveness check.

### F6. Session-level tool friction (from deep-mined sessions)

Two large concurrent feature sessions (grimoire public tier; homepage rack),
6.5h combined, bypassPermissions mode:

- Tool failure rates 3.3% / 4.4% (Bash dominates).
- **Semgrep pre-commit noise**: "Semgrepignore v2" deprecation warnings (28
  lines per run, from slash-containing unanchored `paths:` globs like
  `tests/**` and `*/values.yaml` in `bazel/semgrep/rules/`) poison the commit
  hook's output, burning ~3 min of grep-filtering to confirm no real findings.
  Two of the warned globs were latent coverage bugs: v2 anchoring would have
  silently zeroed the `tests/**` excludes (3 rules would start flagging the
  whole monolith test suite) and the `*/values.yaml` include (PDB rule
  coverage drops to zero).
- **pre-commit format loop**: `format-code` hook modifies files, fails the
  commit, forcing restage+recommit (3 occurrences in 2 sessions). Inherent
  pre-commit semantics, small but per-commit.
- **Partial commits fight the generator pass** (found while landing this
  work): `fast-format.sh --staged` also runs the doc/config generators, which
  rewrite the docs manifests; when a commit stages only a subset of a larger
  change, the regenerated (unstaged) manifests count as "files modified by
  hook" and pre-commit's stash restore conflicts, aborting the commit with no
  committed result. Workaround: stage the full change set (including
  regenerated manifests) before committing. Also: full-repo
  `fast-format.sh` runs with a stale local tools image reformat dozens of
  untouched files (prettier version skew vs the committed state); scope
  formatting to staged files.
- **Hand-rolled ArgoCD API access**: with no ArgoCD MCP, one session ran the
  port-forward + admin-secret + JWT + curl dance 4x in one minute to inspect
  sync state / managed-resources diffs; another polled `kubectl get application`
  6x. The diagnosis recipes exist only in session memory, not as repo tooling.
- **Gotcha rediscovery despite documentation**: the grimoire feature hit 3 of the
  4 gotchas pre-documented in a memory file (gazelle-exclude imports in the
  public binary, missing `/api` same-origin proxy, incomplete `public_reader`
  grants) as three sequential merged PRs (#3167 -> #3171 -> #3174), each
  discovered by post-merge production curl, not pre-merge checks.
- **Oversized CI log fetches**: one `get_invocation` artifact pull exceeded the
  MCP token cap (1.7M chars); the agent explicitly noted "log-chasing is costing
  more than it's worth" before reading BUILD files directly.
- Largest wall-clock gaps in both sessions were human design-review latency
  (18-24 min waiting on mockup feedback), not agent stalls: the fixable agent
  cost is concentrated in the deploy dance and gotcha rework above.

Four further sessions (WhatsApp gateway, runtime recipes, goosecracker fixes,
fc-invoke auth; ~10h combined active) corroborate and add:

- Tool error rates uniformly low (0.9-2.9%): raw tool failures are NOT the
  dominant friction; process round-trips are.
- **Rollout verification is the largest waiting category**, dwarfing CI-check
  polling: 90+ minutes across 3 sessions confirming a deploy actually landed
  (42 min OutOfSync investigation; a 12-min bounded poll on a stale image plus
  22 min chasing it; 11 min post-merge 401 investigation).
- **ArgoCD stale chart-render cache hit for a third documented time**; the MCP
  sync tool even reported synced:true with no effect, and only an
  `argocd.argoproj.io/refresh=hard` annotation forced the re-pull.
- **Chart-bump merge conflicts in 3 of 4 sessions**: each cost a
  rebase + re-bump + force-push cycle; the Chart.yaml/targetRevision pair is
  the single most conflict-prone line pair in the repo.
- A migration-ordering hook warning fired and was ignored (out-of-order
  timestamp committed anyway): warn-only hooks get skipped under momentum.
- Two new gotchas discovered live: vendored Go deps with pre-generated
  `.pb.go` files fight gazelle proto generation (fix: `gazelle_override` in
  MODULE.bazel); and a two-app PR (fc-invoke + monolith) rolling out at
  different speeds opened a ~10-min 401 window (ship the caller-update PR
  first, then the auth-enable PR).
- The dead-hook signature (exit 126 x2 per edit) appears in every session
  where it was checked: 114+ more silent failures across this batch.

## Cost model

One CI round is cheap (median Test job 46s; full suite < 2 min wall). The real
unit costs are:

| Cost unit                                                | Typical wall time                      | Driver |
| -------------------------------------------------------- | -------------------------------------- | ------ |
| Extra PR (bump-only, or follow-up fix)                   | 2-7 min merge + session attention      | F1, F3 |
| Post-merge rollout convergence + OutOfSync debugging     | 10-60 min when wedged                  | F1     |
| Production-discovered gotcha (fix PR + rollout + verify) | 15-40 min each                         | F3     |
| Failed commit round (format/semgrep hook)                | 0.5-3 min each                         | F2, F6 |
| Dead guardrail                                           | unbounded (the wedge it guards recurs) | F5     |
