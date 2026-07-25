# CLAUDE.md - Secure Kubernetes Homelab

## Repository

Hosted at **https://github.com/jomcgi/homelab**. The `gh` CLI is authenticated.

## Repository Structure

```
homelab/
├── projects/            # All services, operators, websites — colocated with deploy configs
│   ├── platform/          # Cluster-critical infra (ArgoCD, Linkerd, SigNoz, etc.)
│   ├── {service}/         # Each service has chart/, deploy/, backend/ as needed
│   │   ├── chart/         # Helm chart (if custom)
│   │   └── deploy/        # ArgoCD Application, values, kustomization
│   └── home-cluster/      # Auto-generated ArgoCD root kustomization
├── bazel/               # All Bazel build infrastructure (rules, tools, images, semgrep, patches)
├── docs/               # Design docs, ADRs, runbooks (plans live in GitHub Issues)
│   ├── decisions/       # Architecture Decision Records — ls decisions/<category>/
│   └── runbooks/        # Explicit-only procedures (not auto skills)
├── MODULE.bazel         # Bazel dependency management (bzlmod, not WORKSPACE)
└── buildbuddy.yaml      # CI pipeline definition
```

**Languages:** Go, Python, JavaScript, Starlark (BUILD files)

## Engineering Philosophy

**Simplest approach first.** Before implementing anything non-trivial, list 2-3 candidate approaches ranked by complexity. Pick the simplest unless you can justify why it's insufficient in one sentence. Wait for an OK on the choice before writing code.

Skip this for: one-line config fixes, typo corrections, mechanical renames, or when the user has already specified the approach. It's for genuine design choices — state machines vs flags, runtime introspection vs lambdas, separate index vs column filter, new framework vs subprocess.

Output shape: "Option A (simplest): …; Option B: … — recommend A unless you want flexibility for X." Then pause.

## Writing Style

**Never use em-dashes (—) in anything you write**: site copy, CV content, docs, ADRs, commit messages, PR bodies, code comments. Use a comma, colon, parentheses, or split the sentence. Existing em-dashes in old files are grandfathered (don't churn files just to strip them), but never add new ones.

## Model Routing & Wall-Time

Objective: lowest wall-time-to-outcome at maximum quality within the weekly
subscription budget. The budget is to be spent, not minimized. Route work to the
cheapest model whose mistakes are caught cheaply; reserve quality tiers for
mistakes only slow CI can catch.

- **Opus 5 for judgment and CI-only-verifiable work**: main loop, code review,
  planning, and subtle or architectural implementation (deep Helm, Bazel/apko,
  RBAC verbs, migration ordering, cross-service wiring). Higher first-pass-correct
  rate avoids expensive CI round-trips.
- **Codex for implementation bulk** (OpenAI subscription, bills off the Claude
  weekly limit). Dispatch ONLY via `bazel/tools/codex/dispatch.sh` (see
  `codex-implement`):
  - **`luna` (default):** most of the value at far lower cost; mechanical *and*
    standard well-specified work
  - **`terra`:** only when Luna is too weak for that task
  - **`frontier` (Sol):** rare cross-vendor second opinion; **never** a default
  One worktree per worker; workers cannot commit, push, or reach the network;
  the dispatching Opus agent reviews, runs `ci`, and commits.
- **Codex quota exhaustion (exit 42):** one Discord notify via
  `monolith-monolith-agent-notify` (level `warn`, main-loop only), then Sonnet
  implementers. No codex retry loop; no re-notify in the same session.
- **Sonnet is the fallback implementer** when Codex is unavailable, out of
  quota, or the task needs Claude-side skills/MCP/context.
- **Opus reviewers are the safety net.** Codex/Sonnet hands, Opus eyes. Do not
  downgrade the reviewer. Sol may be *added* as a second opinion on the hardest
  diffs, never substituted for Opus review.
- **Reject downgrades done purely to save Claude quota** when they buy more CI
  round-trips. Prefer Luna over Sol to save *OpenAI* cost without quality loss
  on bulk work.
- **Fable is last resort only** (`/model fable`). Not a normal-day upgrade path.
  Use only when Opus 5 is stuck on a genuine hard design/debug wall after a real
  attempt; do not open Fable for routine features.
- **Search is cheap.** Prefer Explore (Haiku) for read-only lookup; keep
  `general-purpose` (inherits Opus) for judgment.
- **Prefer parallel fan-out** for independent work (multiple Codex Luna workers
  or subagents in one turn).
- **Write the full task spec up front** so one-shot implementers succeed.
- **Effort default is `high`** (global settings). Bump to `xhigh` only for the
  hardest long-horizon runs; never drop below `high` to save budget.
- **Fast mode (`/fast`) is off by default.** Cash-for-speed bypass only when
  blocked at the weekly wall.

## Essential Commands

```bash
# Local feedback loop (direnv puts `ci` on PATH; else bazel/tools/ci/ci)
ci              # lint changed files + selective regen + bb remote Linux test
ci lint         # format only files changed vs origin/main
ci regen        # generators/gazelle only when inputs changed
ci test         # 1:1 with buildbuddy.yaml Test (see below)

helm template <release> projects/<service>/chart/ -f projects/<service>/deploy/values.yaml  # NEVER helm install
bazel/tools/git/bump-chart.sh projects/<service>  # Chart.yaml + targetRevision together
```

**`ci` is the feedback loop.** Lint/regen are file-selective locally; the test step is [BuildBuddy Remote Bazel](https://www.buildbuddy.io/docs/remote-bazel/) with the **exact** Workflows **Test** argv (so the action cache is shared with PR CI):

```bash
bb remote --os=linux --arch=amd64 \
  test //... --config=ci --deleted_packages=bazel/tools/python --test_tag_filters=-external,-future
```

A green `ci test` should make the PR **Test** check mostly cache-hit.

Do **not** run bare `bazel`/`bazelisk` on the Mac (no darwin workflow executors; wrong platforms). `bb remote` is allowed; prefer `ci`. Image push stays CI-only on merge/push to main.

For multi-task plans: implementers can run `ci` (or `ci test`) before handoff; still push the branch so required GitHub checks run. Inspect failures via `mcp__buildbuddy__*` if needed.

**Vendored tools** (`./bootstrap.sh` + `direnv allow`): `ci`, `helm`, `crane`, `kind`, `go`, `python`, `pnpm`, `node`, `buildifier`, `buildozer`, `ruff`, `gofumpt`, `shfmt`, `prettier`, `gazelle`, `bb`

## Development Workflow

**NEVER commit directly to main.** All changes MUST go through a worktree + PR.

The main repo at `~/repos/homelab` auto-fetches every 60s — always use worktrees for active development.

1. `git -C ~/repos/homelab worktree add -b feat/my-feature /tmp/claude-worktrees/my-feature origin/main`
2. Make changes in `/tmp/claude-worktrees/my-feature`
3. Run `ci` (lint + regen + remote test) until green
4. Commit, push, create PR (PR checks should mostly cache-hit)
5. Merge after required checks pass

**PR merge method:** This repo only allows **rebase merging** — use `gh pr merge --rebase` (or `--auto --rebase`). Squash and merge commits are disabled.

**Auto-merge for small bug fixes:** For small, focused fixes (e.g. one-line config changes, typo fixes), enable auto-merge with `gh pr merge --auto --rebase`. After enabling, follow through:

1. Poll `gh pr view <number> --json state,mergeStateStatus` until CI passes and the PR merges
2. Poll the rollout (via MCP tools) to verify the fix is live and working

If `gh pr merge --auto --rebase` fails with "Pull request is in clean status", the PR is already green: merge directly with `gh pr merge --rebase`. Prefer `run_in_background` Bash or Monitor over `sleep`-chained polling for CI waits (sleep-chains are blocked anyway).

**Required checks are STRICT: the branch must be up to date with main.** Whenever any other PR merges, every open PR goes `BEHIND` and cannot merge (auto-merge will not fire) until you update it: `gh pr update-branch <number> --rebase`, then let CI re-run. This is deliberate, not friction to route around: the re-run makes the missed-chart-bump guard re-check against post-merge main, which is the only point a rebase-merge version collision (two PRs claiming the same chart version, the loser's bump silently dropped) is detectable. If `mergeStateStatus` is `BEHIND`, update the branch; never try to bypass the strict check.

**PR safety:** Always verify PR state (`gh pr view --json state`) before pushing additional commits. Never push to a merged branch — create a new worktree instead.

**Work tracking (GitHub Issues are the source of truth).** Outstanding work in this repo (bugs, features, TODOs) is tracked in **GitHub Issues**, not in ADRs or committed plan docs. ADRs (`docs/decisions/`) record the *decision and rationale* and are supporting detail; the *implementation approach* for a piece of work lives in its GitHub issue (or the feature's tracking issue), not in a committed plan file (`docs/plans/` is retired, see "Plans are not committed"). What is *left to do* lives in an issue. When an ADR surfaces unimplemented work, file it as an issue (label `bug`/`enhancement`/`documentation`, add `agent-ready` when autonomously pickable) titled `<area>: <summary>` (append `, ADR <cat>/<NNN>` when ADR-derived) and linking back to the ADR. Decompose a multi-part initiative into a parent tracking issue with **sub-issues** (`gh api repos/jomcgi/homelab/issues/<parent>/sub_issues -F sub_issue_id=<childDatabaseId>`, note `-F` for the integer field, `-f` sends a string and 422s). Closing the issue, not editing the ADR, is how "shipped" is recorded.

**Commit messages MUST use [Conventional Commits](https://www.conventionalcommits.org/) format.** A `commit-msg` hook enforces this.

Format: `<type>(<optional scope>): <description>`

Allowed types: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`, `test`, `build`, `ci`, `chore`, `revert`

Examples:

- `feat: add health check endpoint for auth service`
- `fix(signoz): correct trace sampling configuration`
- `ci: add conventional commit pre-commit hook`
- `docs: update observability runbook`

Breaking changes: add `!` after type/scope — `feat!: redesign auth token format`

**Plans are not committed to the repo.** `docs/plans/` is retired. The brainstorming and writing-plans skills still produce a plan for the work at hand, but that plan lives in the feature's GitHub issue (or the tracking issue for a multi-step initiative), or as an uncommitted working file, never as a committed `docs/plans/*.md`. GitHub Issues are the source of truth for outstanding work. A PreToolUse hook blocks writes under `docs/plans/` to enforce this.

**Code review cadence for multi-step plans.** When executing a multi-task implementation plan (subagent-driven, executing-plans, or any multi-commit feature), do **one comprehensive code review per merged PR** at the end of the implementation — not per sub-task or per commit. Per-task spec/quality reviewer dispatches accumulate cost disproportionate to their value when an end-of-PR review catches the same issues against the full diff. Implementer subagents should still self-review before each commit; that's where intra-task discipline lives. This overrides the per-task review steps in the `superpowers:subagent-driven-development` skill — that skill was calibrated for an autonomous setting; in our workflow the PR is the review boundary.

**Plan execution defaults to subagent-driven + Codex Luna.** After producing a plan (held in the tracking issue or an uncommitted working file, never `docs/plans/`), do **not** present a mode-choice prompt. Announce "starting Task 1," dispatch implementers (prefer **Codex Luna** via `codex-implement`; Sonnet only as fallback), run **`ci`** before push, Opus reviews the PR. Only ask about mode if the user wants a detached overnight run or the plan has unusual properties.

## Context Loading Rules

- **Security changes**: Read `docs/security.md` FIRST
- **Public tier changes** (jomcgi.dev pages, monolith-public, `public_reader` data): Read `docs/runbooks/public-tier-checklist.md` FIRST
- **ArgoCD OutOfSync / stuck rollout / "is my change live?"**: Read `docs/runbooks/argocd-outofsync.md`
- **New services**: Read `docs/contributing.md` + `docs/reference/services.md`
- **Observability work**: Read `docs/observability.md`
- **Alerting work**: Read `docs/reference/observability-alerting.md`
- **Operator changes**: Read `projects/operators/best-practices.md`
- **Design proposals**: Check `docs/decisions/` for ADRs (numbered per category)

### Skills vs runbooks

- **Skills** (`.claude/skills/`): only four auto-matchable workflows: `ship`,
  `adr`, `stpa`, `codex-implement`.
- **Runbooks** (`docs/runbooks/`): **explicit-only** (knowledge, improve-*,
  scheduler, digests, bazel/apko patterns, etc.). Open only when Joe asks, a
  rule above names the file, or a `claude_routines/*.yaml` prompt points at the
  path. Index: `docs/runbooks/README.md`. Do not invent skills for runbook
  content. Image builds and `bazel test` run via **`ci` / Workflows**, not
  local bare `bazel`; the bazel/apko runbooks are pattern docs, not a local
  test loop.

## Key Patterns

| Pattern                    | Implementation                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| -------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Secrets**                | 1Password Operator (`OnePasswordItem` CRD) — never hardcode                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| **Container images**       | apko + rules_apko (not Dockerfiles) — always dual-arch (x86_64 + aarch64)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| **Deploy / rollout**       | We do NOT use ArgoCD Image Updater. Releases roll out by building dual-arch OCI images in CI and bumping the chart version: CI rebuilds the image with a build-time-pinned tag, and a new chart version (Chart.yaml + `deploy/application.yaml` `targetRevision`, bumped together by `bazel/tools/git/bump-chart.sh` in the author's PR) is what ArgoCD pulls from OCI and syncs. A legacy chart-version-bot auto-bump still runs on PR branches but is being retired (see ADR platform/009); do not rely on it.                                                                                                                        |
| **Image pinning**          | Bazel `helm_images_values` deep-merges pinned tags into `values.yaml` at build time — never manually set `@sha256:` digests in deploy values files                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| **Package deps (Python)**  | `@pip//package` via aspect_rules_py (not `requirement()`)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| **Package deps (JS)**      | pnpm + rules_js                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| **Non-root containers**    | uid 65532 convention, `runAsNonRoot: true`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| **Helm service names**     | Helm prepends `<release-name>-` to service names. A service `web` in release `myapp` is reachable at `myapp-web.<namespace>.svc.cluster.local`. Never hardcode these URLs in Go application defaults — inject from `values.yaml` env vars.                                                                                                                                                                                                                                                                                                                                                                                              |
| **Chart version bumps**    | Bump with `bazel/tools/git/bump-chart.sh projects/<svc>`: it updates `chart/Chart.yaml` `version` AND `deploy/application.yaml` `targetRevision` together, numbering from the origin/main tip so concurrent sessions cannot pick the same version. Any PR whose code must deploy needs the bump in the SAME PR. Main-branch chart publish is idempotent: an already-published version is never re-pushed (mutating it in place used to wedge ArgoCD), and a merge whose changes needed a bump fails the `Push images` action loudly with the exact fix command, so a missed bump surfaces in minutes instead of as a silent non-deploy. |
| **RBAC for new endpoints** | New monolith endpoints that read or list cluster resources (Argo apps, deployments, pods, etc.) require corresponding `ClusterRole` rules. Verify the RBAC manifest covers every verb (`get`/`list`/`watch`) the new code calls before merging. Missing verbs fail silently in prod with `Forbidden` errors that look like generic 5xx in dashboards. Most recent example: `bc59d5f0c` granted `get` on `argoproj.io/applications`.                                                                                                                                                                                                     |

## Cluster Investigation

**`kubectl` is the primary tool for cluster reads.** The kubernetes, ArgoCD, and SigNoz MCP servers are no longer registered — as of 2026-06-12 Context Forge serves only the `GitHub` and `monolith` gateways (confirmed by querying its Postgres). The MCP servers that ARE available:

- **monolith** (`monolith-*` / `monolith-monolith-agent-*` tools) via Context Forge / the `claude_ai_homelab` remote server, prefixed `mcp__claude_ai_homelab__`.
- **buildbuddy** (`mcp__buildbuddy__*`) via the project-scoped `.mcp.json` (needs `${BUILDBUDDY_API_KEY}`).

For everything else, use `kubectl`. SigNoz observability (historical logs/traces/metrics) lives in the SigNoz UI at `private.jomcgi.dev/app/signoz`; a local-only `signoz-mcp-wrapper.sh` (kubectl port-forward + stdio MCP) can expose `signoz-*` tools if you run it manually, but nothing registers it by default. Don't burn turns on `ToolSearch +kubernetes`/`+argocd`/`+signoz` — those tools aren't there.

**BuildBuddy MCP setup:** The repo includes a project-scoped `.mcp.json` that auto-registers the BuildBuddy MCP server (`https://jomcgi.buildbuddy.io/mcp`) using `${BUILDBUDDY_API_KEY}` from your shell env. Set that env var (e.g. in `~/.zshrc`) before starting a Claude Code session in this repo — without it, the `mcp__buildbuddy__*` tools won't load and there's no fallback path for inspecting CI runs.

| Need                                                      | Tool                                                                                                                                                                                                                                                                                                                                                                                                                          |
| --------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **K8s resources**                                         | `kubectl get`/`kubectl describe -n <ns>`                                                                                                                                                                                                                                                                                                                                                                                      |
| **K8s logs**                                              | `kubectl logs -n <ns>` (recent); SigNoz UI for historical                                                                                                                                                                                                                                                                                                                                                                     |
| **K8s metrics**                                           | `kubectl top pods -n <ns>`, `kubectl top nodes`                                                                                                                                                                                                                                                                                                                                                                               |
| **ArgoCD apps**                                           | `kubectl get applications -n argocd` (+ `-o yaml` for sync/health). No ArgoCD MCP exists; the UI is at `private.jomcgi.dev/app/argocd`.                                                                                                                                                                                                                                                                                       |
| **BuildBuddy CI**                                         | `mcp__buildbuddy__get_invocation` (selectors: `invocationId` or `commitSha`) → `get_target` → `get_action` → `get_log`. `get_file_range` reads byte ranges from CAS blob URIs in build events (16 MiB max).                                                                                                                                                                                                                   |
| **Observability** (logs/traces/metrics/dashboards/alerts) | SigNoz UI at `private.jomcgi.dev/app/signoz`. No MCP by default; running `projects/platform/signoz-addons/signoz-mcp-wrapper.sh` locally exposes `signoz-*` tools (search-logs, search-traces-by-service, list-dashboards, list-alerts, etc.).                                                                                                                                                                                |
| **Agent jobs**                                            | `monolith-monolith-agent-list-routine-jobs`, `monolith-monolith-agent-trigger-routine-job`, `monolith-monolith-agent-trigger-job`                                                                                                                                                                                                                                                                                             |
| **Alert / message Joe**                                   | `monolith-monolith-agent-notify` posts a Discord message via the in-process bot (args: `message`, optional `level` of `info`/`warn`/`error`, optional `channel` from the allow-list). Defaults to the homelab channel. This is the way to reach Joe from a session (a finished long task, a blocked decision, a heads-up). Outbound only: it cannot read or list channels. Be sparing, one clear message, not a play-by-play. **Single voice: only the top-level (main-loop) agent may send this. Subagents must NEVER Discord-notify Joe; they surface blockers, design forks, and questions to the dispatching agent (via their final report or SendMessage), and the main agent decides whether it warrants reaching Joe. This keeps Joe hearing one voice instead of parallel pings from fan-out workers.** |

**Ending a turn while blocked.** When you stop a turn waiting on Joe's input (an `AskUserQuestion` he hasn't answered, an approval you need before proceeding, or a decision only he can make) and he may be away from the session, send one `monolith-monolith-agent-notify` message so the blocker actually reaches him instead of sitting silently in a session he isn't watching. Keep it to a single line stating what you need and why you're stopped. Do not notify for routine turn endings where no decision is pending, and do not double-notify the same blocker. This is a **main-loop-only** action (see the single-voice rule above): a spawned subagent that hits a fork or question reports it back to the dispatching agent rather than notifying Joe itself, and the main agent resolves it inline where it can, escalating to Discord only for decisions that are genuinely Joe's.

## Kubernetes Operations (kubectl)

**CRITICAL: This cluster is managed via GitOps.**

**FORBIDDEN** — modify Git instead: `apply`, `patch`, `edit`, `scale`, `delete`

To make changes: edit `projects/<service>/deploy/values.yaml` → commit → push → ArgoCD auto-syncs (~5-10s).

## GitOps Application Structure

Services are colocated in `projects/{service}/deploy/`:

- `application.yaml` — ArgoCD Application pointing to the service's chart with Helm values
- `kustomization.yaml` — Makes app discoverable (`resources: [application.yaml]`)
- `values.yaml` — Cluster-specific Helm value overrides

ArgoCD root is `projects/home-cluster/kustomization.yaml` (auto-generated by `bazel/images/generate-home-cluster.sh`).

**Adding a new service:** copy a recent service's `deploy/` directory (e.g. `projects/monolith/deploy/`) as the template — note the multi-source pattern (OCI chart from `ghcr.io/jomcgi/homelab/charts` with pinned `targetRevision`, plus a `$values` git ref for deploy values). Adjust names/namespace, run `format` (regenerates the home-cluster root kustomization), commit.

## Continuous Integration

CI uses **BuildBuddy Workflows** (not GitHub Actions). Defined in `buildbuddy.yaml`.

All builds run **remotely** via BuildBuddy RBE — `bazel`/`bazelisk` is aliased to the BuildBuddy CLI (`bb`).

Runs on every push/PR:

- **Format check** — standalone formatters + gazelle, auto-commits fixes on PR branches (as `ci-format-bot`)
- **Test and push** — `bazel test //...`, pushes images on main branch

**Inner loop is `ci`, then push.** Run `ci` (or at least `ci test`) before pushing so PR Workflows mostly cache-hit. Pre-push hook runs `ci test` when installed (`pre-commit install --hook-type pre-push`); skip with `SKIP_CI_TEST=1` for docs-only. After push, monitor with `gh pr checks <number> --watch`. Read failures via `mcp__buildbuddy__get_invocation` + `get_log` (see Cluster Investigation table).

**Ship / multi-phase work:** lifecycle + Phase 4 gates live on the **GitHub tracking issue** (see `ship` skill template): green `ci`, `Closes #<issue>`, chart bump if deployable, up-to-date with main, review. Do not call a PR ready until those boxes are checked on the issue.

**CI failure diagnosis — quote before hypothesizing.** When CI is red, the first action is to fetch the actual log: `mcp__buildbuddy__get_invocation` (use `commitSha` selector to skip the invocation-ID lookup) → `get_target` to find failing targets → `get_log` for the trace.

Quote the actual assertion error or exception message verbatim before proposing a cause. Do **not** mention infrastructure issues (BuildBuddy outages, flaky runners, RBE hiccups) unless a real test failure has been ruled out — Claude has hallucinated infra failures here before, and the cost of one wrong "it's just flaky" is several wasted iterations.

**Bumping config values that tests assert on.** When changing a TTL, timeout, `max_tokens`, retry count, or any numeric config, `grep` the test tree for the old value first and update assertions in the same commit. Otherwise CI fails on the test, you (or I) misattribute it to flakiness, and the fix takes a second push.

## Anti-Patterns

- **Using Dockerfiles** — this repo uses apko exclusively for container images
- **Running as root** — always use non-root (uid 65532)
- **Direct internet exposure** — all traffic goes through Cloudflare
- **Bare `bazel test` on the Mac** — use `ci` / `bb remote` (Linux Remote Bazel) instead; no darwin workflow executors
- **Using `@rules_python` syntax** — this repo uses `@aspect_rules_py`
- **Building a custom Helm chart when upstream provides one** — always check the upstream project repo for an existing chart before creating a custom one
- **Hardcoding `.svc.cluster.local` URLs in Go defaults** — when a Helm release is renamed the service name prefix changes silently; set via `envOr("URL", "")` (no default) and configure in `values.yaml`; semgrep rule `no-hardcoded-k8s-service-url` catches this in CI
- **Manually pinning `@sha256:` image digests in values files** — digests go stale after CI rebuilds, causing `ImagePullBackOff`; the Bazel pipeline manages pinning automatically; semgrep rule `no-hardcoded-image-digest` catches this in CI
- **Bumping `Chart.yaml` without `application.yaml`** — use `bazel/tools/git/bump-chart.sh`, which updates both together; a mismatch means ArgoCD keeps deploying the old chart version with stale image digests (an edit-time hook warns when the two drift)
- **Putting large data in the migrations ConfigMap** — `chart/migrations/*.sql` are Helm-globbed into the `monolith-migrations` ConfigMap, which the Atlas operator reads. ArgoCD applies it client-side, storing the whole object in the `last-applied-configuration` annotation, hard-capped at 256 KiB. A big migration (e.g. a bulk data seed) silently breaks sync with `metadata.annotations: Too long`. Keep seeds/bulk data out of migrations — load them out-of-band (see `projects/monolith/hikes/seed/`)
- **Over-engineering** simple services
