# CLAUDE.md - Secure Kubernetes Homelab

A single-cluster Kubernetes homelab at
[jomcgi/homelab](https://github.com/jomcgi/homelab). Services, operators, and
websites live under `projects/<name>/`, each colocating its Helm `chart/` with
the `deploy/` config ArgoCD ships it from. Everything builds with Bazel (bzlmod,
not WORKSPACE) and deploys from Git. Go, Python, JavaScript, and Starlark. The
`gh` CLI is authenticated.

Use `ls` for structure and `git log` for history. The rest of this file is the
things you would only learn by breaking them.

## Invariants

- **The cluster is GitOps, so `kubectl` is read-only.** Never `apply`, `patch`,
  `edit`, `scale`, or `delete`. To change something, edit
  `projects/<service>/deploy/values.yaml`, commit, push, and ArgoCD syncs within
  5 to 10 seconds.
- **Never commit to main.** Worktree, branch, PR, rebase-merge, and this repo
  allows rebase merges only. See the `pr-workflow` skill.
- **`ci` is the feedback loop, not PR CI.** Run it before pushing.
- **No em-dashes in anything you write**: site copy, CV content, docs, ADRs,
  commit messages, PR bodies, code comments. Use a comma, colon, parentheses, or
  split the sentence. Existing ones are grandfathered, so do not churn files to
  strip them.
- **Conventional Commits**, enforced by a `commit-msg` hook.
- **GitHub Issues are the source of truth for outstanding work**, not ADRs and
  not committed plan files. `docs/plans/` is retired and a hook blocks writes
  there. ADRs record the decision and its rationale; the issue records what is
  left to do.

## Working style

Reach for the simplest approach that holds. On a genuine design fork (state
machine vs flags, runtime introspection vs lambdas, new framework vs subprocess),
put two or three options in front of Joe ranked by complexity with a
recommendation, then wait. Skip that for config fixes, renames, and anything he
has already scoped.

Once a plan exists, do not offer a mode-choice prompt: announce Task 1, dispatch
implementers, run `ci`, and review as Opus. Do **one** comprehensive review per
PR at the end rather than per sub-task; the PR is the review boundary. That
overrides the per-task review steps in
`superpowers:subagent-driven-development`, which was calibrated for a more
autonomous setting.

Completion claims are verified, not trusted. When an implementer or teammate
agent reports a commit or an edit, check the artifact itself (`git show
--stat`, re-read the file) before building on it or reporting it up. A merged
PR whose change must deploy is done only when the rollout is verified live;
the `pr-workflow` skill has the checklist. When debugging, state the
hypothesis and run the one command that would falsify it before writing any
fix. For site copy and CV prose, audit facts and flag unsupportable claims,
but offer at most one draft: Joe writes the final wording.

If you end a turn blocked on a decision only Joe can make and he may be away,
send one `monolith-monolith-agent-notify` line saying what you need. Main loop
only: subagents report blockers to their dispatcher, never to Discord.

## Model routing

The weekly subscription is the binding constraint, so routing is not a
preference, it is how the budget lasts. Three lanes:

- **Opus orchestrates.** Main loop, planning, code review, and the slices only a
  slow CI round-trip would catch: deep Helm, Bazel/apko, RBAC verbs, migration
  ordering, cross-service wiring. Opus reviews every PR, and the reviewer never
  gets downgraded to save quota.
- **Luna implements, and that should be around 99% of implementation.** Dispatch
  through the `codex-implement` skill (`bazel/tools/codex/dispatch.sh`), which
  bills OpenAI instead of the Claude weekly limit. Terra only after Luna has
  actually failed on that task. Sonnet only when Codex is unavailable or the work
  genuinely needs Claude-side skills, MCP, or session context. On exit 42 (quota
  exhausted), send one `warn` notify and fall back to Sonnet: no retry loop, no
  second notify.
- **Fable is a last-resort escalation for context window, not for quality.**
  Open it when Opus has stalled and the blocker is running out of context, not
  when the problem is merely hard.

Dispatches run in the background (they take minutes to over an hour) and
correction rounds are batched: after a review or a red `ci`, one consolidated
respec covering every finding, never one dispatch per finding. If review
already produced the exact diff (under ~20 lines), apply it directly; the
thinking is spent and dispatch overhead exceeds the typing.

**If you are about to write implementation code in the main loop, that is the
signal to dispatch instead.** Writing it yourself is the default failure mode,
and it is the expensive one. Spawn the **`implementer`** agent
(`.claude/agents/implementer.md`), which has no `Write` or `Edit` tool and can
only hand the spec to a Codex worker, so the routing holds by construction
rather than by memory. Give it the full spec and a worktree; it returns the
diff. If it reports `CODEX_QUOTA_EXHAUSTED`, that is the exit 42 path above.

If a session has no `codex` on PATH, dispatch is unavailable: use that session
for orchestration, review, and triage rather than quietly implementing on Opus.

Context is the other half of the same budget:

- Search with Explore (Haiku). `general-purpose` inherits Opus, so keep it for
  judgment, never for lookup.
- Fan out independent work across workers in one turn rather than serializing it.
- Write the full task spec up front. A one-shot implementer that has to come back
  for clarification costs more than the spec would have.
- Effort stays `high`. Bump to `xhigh` for the hardest long-horizon runs, never
  drop below `high` to save budget.
- `/fast` is off by default: cash-for-speed only when blocked at the weekly wall.

## Commands

```bash
ci              # lint changed files + selective regen + bb remote Linux test
ci lint         # format only files changed vs origin/main
ci regen        # generators and gazelle, only when inputs changed
ci test         # 1:1 with the buildbuddy.yaml Test action

helm template <rel> projects/<svc>/chart/ -f projects/<svc>/deploy/values.yaml  # NEVER helm install
```

`ci test` runs the exact Workflows Test argv through BuildBuddy Remote Bazel, so
the action cache is shared with PR CI and a green `ci test` makes the PR Test
check mostly cache-hit. Bare `bazel` / `bazelisk` on the Mac is wrong (no darwin
workflow executors, wrong platforms); `bb remote` is allowed, `ci` is better.
Image push stays CI-only on merge to main.

Tooling is vendored: `./bootstrap.sh` then `direnv allow` puts `ci`, `helm`,
`crane`, `kind`, `go`, `python`, `pnpm`, `node`, `bb`, and the formatters on PATH.

## Gotchas

- **Deploys are triggered by chart version, not by image tags, and the version
  is written AFTER merge.** There is no ArgoCD Image Updater. CI rebuilds
  dual-arch images with a build-time-pinned tag, and ArgoCD only picks them up
  when the chart version moves. Since ADR platform/009 decision 1 that move
  happens on `main`, not on the branch: **a PR must not touch `Chart.yaml`
  `version:` or `deploy/application.yaml` `targetRevision:` at all.** Main's
  publish computes the next version from the merged history, pushes the OCI
  chart, and commits both lines back as `chart-version-bot`. So a deploying PR
  now carries no bump, and the deploy lands one commit after the merge rather
  than in it. If a change has not rolled out, check that write-back commit
  landed before assuming a missed bump.
- **Never hand-pin `@sha256:` digests in values files.** Bazel
  `helm_images_values` deep-merges pinned tags at build time. Hand-pinned digests
  go stale after the next CI rebuild and turn into `ImagePullBackOff`. Semgrep
  `no-hardcoded-image-digest` catches it.
- **Helm prepends the release name to service names.** A service `web` in release
  `myapp` resolves at `myapp-web.<namespace>.svc.cluster.local`, so a release
  rename silently breaks any hardcoded URL. Never hardcode one in a Go default:
  use `envOr("URL", "")` with no default and set it in `values.yaml`. Semgrep
  `no-hardcoded-k8s-service-url` catches it.
- **New monolith endpoints that read cluster resources need matching
  `ClusterRole` verbs.** Check every `get` / `list` / `watch` the code calls
  before merging. Missing verbs fail silently in prod as `Forbidden`, which shows
  up in dashboards as a generic 5xx.
- **Keep bulk data out of the migrations ConfigMap.** `chart/migrations/*.sql`
  are Helm-globbed into `monolith-migrations`, which ArgoCD applies client-side,
  storing the whole object in the `last-applied-configuration` annotation, hard
  capped at 256 KiB. A bulk seed breaks sync with `metadata.annotations: Too
  long`. Load seeds out of band (see `projects/monolith/hikes/seed/`).
- **Grep the tests before changing a number.** TTLs, timeouts, `max_tokens`, and
  retry counts get asserted on. Update the assertions in the same commit or CI
  fails in a way that reads like flakiness.
- **Never pipe `ci` / `bb remote` output straight into a filter** (`tail`,
  `head`, `grep`, `sed`, `awk`, `wc`) **or discard it** (`>/dev/null`); a
  hook blocks it. Run unpiped, or `| tee` to a file and inspect the saved
  log. Truncated reads are how false-green reports happen, and `ci test`
  has exited 0 without running anything (#4118). Judge a run by its
  `Executed N out of M tests` summary line, never by exit code alone.
- **Images are apko plus `rules_apko`, never Dockerfiles**, always dual-arch
  (x86_64 and aarch64), always non-root on uid 65532 with `runAsNonRoot: true`.
- **Python deps are `@pip//package` via `aspect_rules_py`.** This repo does not
  use `@rules_python`, so `requirement()` syntax is wrong. JS is pnpm plus
  `rules_js`.
- **Secrets come from the 1Password Operator** (`OnePasswordItem` CRD). Never
  hardcode one.
- **Nothing is exposed to the internet directly.** All traffic goes through
  Cloudflare.
- **The kubernetes, ArgoCD, and SigNoz MCP servers do not exist.** Context Forge
  serves only the GitHub and monolith gateways, so do not spend turns on
  `ToolSearch +kubernetes`, `+argocd`, or `+signoz`. Use `kubectl`, and the UIs
  at `private.jomcgi.dev/app/argocd` and `private.jomcgi.dev/app/signoz`. The
  BuildBuddy MCP needs `${BUILDBUDDY_API_KEY}` in the shell env *before* the
  session starts, and there is no fallback for inspecting CI without it.
- **New service:** copy a recent `deploy/` directory (`projects/monolith/deploy/`
  is a good template) for the multi-source pattern, an OCI chart from
  `ghcr.io/jomcgi/homelab/charts` with a pinned `targetRevision` plus a `$values`
  git ref. Adjust names and namespace, then run `ci regen` to regenerate the
  ArgoCD root at `projects/home-cluster/kustomization.yaml`.

## Where to look next

| When | Read |
|------|------|
| Security-sensitive change | `docs/security.md` first |
| Public tier: jomcgi.dev pages, monolith-public, `public_reader` data | `docs/runbooks/public-tier-checklist.md` first |
| ArgoCD OutOfSync, stuck rollout, "is my change live?" | `docs/runbooks/argocd-outofsync.md` |
| Adding a service | `docs/contributing.md`, `docs/reference/services.md` |
| Observability work | `docs/observability.md` |
| Alerting work | `docs/reference/observability-alerting.md` |
| Frontend or design work: tokens, palette, motion, a11y | `.impeccable.md` (three scoped design systems, deliberately not converged) |
| Operator changes | `projects/operators/best-practices.md` |
| How a domain works today | `projects/<domain>/ARCHITECTURE.md` where one exists (`embervm` today). Source of truth for current state, and what to link to. The ADRs are rationale |
| Design proposals and decision rationale | `docs/decisions/`, numbered per category. Never cite one as current state: an ADR records what was decided, not what shipped |
| Rolling a domain's ADRs up into `ARCHITECTURE.md` | `docs/runbooks/rollup-architecture-docs.md` |

**Skills** (`.claude/skills/`, auto-matched): `ship`, `adr`, `stpa`,
`codex-implement`, `pr-workflow`, `ci-triage`, `improve-buildbuddy-usage`.

**Agents** (`.claude/agents/`): `implementer` (Luna), `reviewer` (Opus),
`adr-author` and `stpa-analyst` (Sonnet), `escalation` (Fable). Dispatch work to
these rather than doing it in the main loop: each gets its own context, and the
three that must not write code have no `Write` or `Edit` tool.

**Runbooks** (`docs/runbooks/`) are explicit-only. Open one when Joe names it,
when a row above points at it, or when a `claude_routines/*.yaml` prompt does.
Index: `docs/runbooks/README.md`. Do not turn runbook content into skills.
