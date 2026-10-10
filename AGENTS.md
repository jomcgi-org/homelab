# AGENTS.md

Instructions for every agent working in this repo, whatever the tool or model.
There is deliberately no CLAUDE.md: Claude Code reads this file natively. The
factory decides which model does which work.

A Kubernetes homelab at
[jomcgi-org/homelab](https://github.com/jomcgi-org/homelab). The GKE hub
(`homelab-hub`) runs every workload and is the only management plane.
`projects/home-cluster/` is residual configuration: do not deploy to it, and do
not tear it down without an issue. `projects/platform/ARCHITECTURE.md` has the
shape. Services, operators and websites live under `projects/<name>/`, each
colocating its Helm `chart/` with the `deploy/` config ArgoCD ships it from.
Everything builds with Bazel (bzlmod) and deploys from Git. Go, Python,
JavaScript and Starlark.

This file is what you would only learn by breaking it. **(gated)** rules fail
loudly in CI or a git hook. Gotchas marked "Gated" are caught by
`bazel/tools/ci/source_ratchet.py`, which fails a PR adding a new instance
(existing ones are grandfathered); a genuinely safe line can opt out with
`ratchet-allow: <rule> (<reason>)` in a comment.

## Invariants

- **The cluster is GitOps, so `kubectl` is read-only.** Never `apply`, `patch`,
  `edit`, `scale`, `label` or `delete`, and never `helm install` or
  `helm uninstall`. Change `projects/<service>/deploy/values.yaml` through a
  PR and ArgoCD syncs it. Gated for scripts (`source_ratchet.py`) and Claude
  Code's shell (a hook) only.
- **Never commit to main (gated: `protect-main.sh`).** Worktree, branch, PR.
  Merging is covered under "Git and PRs" below.
- **Never move a chart's `version:` or pinned `targetRevision:` forward (gated:
  `bazel/tools/ci/chart_version_guard.py`).** Main's publish computes the next
  version after merge and `chart-version-bot` writes both lines back, so a
  deploy starts at that write-back commit: check it landed before assuming a
  rollout failed. Lowering a pin is allowed: it is the revert lever.
- **`ci` is the feedback loop, not PR CI.** Run it before pushing. If `ci` is
  not on your PATH (a sandboxed guest, say), PR CI is your test run and your
  task spec says how to deliver.
- **Conventional Commits (gated: `commit-msg` hook).**
- **No em-dashes in anything you write**, commits and PR bodies included. Use
  a comma, colon or parentheses. Do not churn files to strip existing ones.
- **GitHub Issues are the source of truth for outstanding work**, not committed
  plan files. The domain's `ARCHITECTURE.md` records a decision and its
  rationale; the issue records what is left to do.
- **Secrets come from the 1Password Operator** (`OnePasswordItem` CRD). Never
  hardcode one. Nothing is exposed to the internet directly: all traffic goes
  through Cloudflare.

## How to work

- A task that modifies tracked files runs in a dedicated worktree on a new
  branch and ends as a pushed PR: ready when validation passes, draft when it
  does not or a question is open. Report the PR URL and state. If you cannot
  open the PR, stop and report the blocker.
- Reach for the simplest approach that holds. On a genuine design fork, put
  two or three options in front of Joe ranked by complexity with a
  recommendation, then wait. Skip that for config fixes, renames, and anything
  already scoped.
- Verify completion claims (`git show --stat`, re-read the file) before
  building on or reporting another agent's work.
- A change that must deploy is done only when the rollout is verified live.
  With the monolith MCP tools, call `verify_deployment` (its description has
  the rules), poll while it says `in_progress`, and report its verdict.
  Without them: the Application is Synced and Healthy, the pod rolled to the
  new image, and the service answers. Never report success on a subset.
- When debugging, state the hypothesis and run the one command that would
  falsify it before writing any fix. On red CI, quote the real failing
  assertion from the log before proposing a cause, and never retrigger a run
  before reading it.
- For site copy and CV prose, audit facts and flag unsupportable claims, but
  offer at most one draft: Joe writes the final wording.
- Blocked on a decision only Joe can make while he may be away: send one
  `monolith-monolith-agent-notify` line, if you have the MCP tools. Subagents
  report blockers to their dispatcher instead.

## Git and PRs

- Merge through the GitHub merge queue with `gh pr merge <n> --auto`. Pass no
  strategy flag: the queue sets it (rebase only) and refuses `--rebase`. A
  queued PR reads `autoMergeRequest: null`; check the queue with GraphQL
  (`pullRequest(number:<n>){mergeQueueEntry{state}}`).
- Never rebase a PR, or `gh pr update-branch`, only because main moved: the
  queue does that. `DIRTY` or `CONFLICTING` is the one case to rebase yourself,
  and `--auto` silently enqueues nothing while it lasts.
- A red queue run ejects the PR. Read the failure, then re-enqueue.
- `homelab pr land <n>` runs that whole flow where the `homelab` CLI is on
  PATH: enqueue with bare `--auto`, wait for the merge or ejection, wait for
  the chart write-back, then poll the rollout verdict for each app the PR
  touched (`--app` to override). It exits non-zero on ejection, a failed
  rollout or its timeout; read the failure it names before acting.
- Never push to a merged branch; start a new worktree. After a push, confirm
  `gh pr view <n> --json headRefOid` equals `git rev-parse HEAD`.
- Issues are titled `<area>: <summary>`, labelled `agent-ready` when an agent
  can pick one up alone. Multi-part work gets a parent with sub-issues
  (`gh api repos/jomcgi-org/homelab/issues/<parent>/sub_issues -F
  sub_issue_id=<child database id>`; `-F`, because `-f` sends a string and
  returns 422). Closing the issue records "shipped".

## Human ownership of factory work

Label an issue `human` when Joe or another person takes ownership, including
implementation, coordination or observation acceptance. This label excludes
both delivery and advisory refinement even when `agent-ready` is present.
Add it before removing `agent-ready`; leave a comment naming the owner, why
work is handed off and what remains. Assignment is useful but not required.
`needs-human` means the factory needs a decision; `human` means a person owns
the work. Agents must not add or remove `human` without an explicit handoff
instruction, relabel owned work `agent-ready`, or start competing work.

The factory fences new starts after observing `human`, reconciles existing
attempts and their costs, then records cancellation with `human_handoff`
evidence. This is neither a delivery success nor an escalation. Unknown
invocations and reservations remain fenced until normal reconciliation proves
their outcome. A handoff is sticky for an admitted receipt: removing the label
does not resume it. Return work through explicit factory re-admission after
the old task settles; removing `human` only restores intake eligibility.
Issues and existing PRs stay open for the human owner.

## Commands

```bash
ci              # lint changed files + selective regen + remote Linux test
ci lint         # format only files changed vs origin/main
ci regen        # generators only, and only when inputs changed
ci test         # affected tests on one hosted Linux runner
ci test -- //...  # explicit full-suite escape hatch

helm template <rel> projects/<svc>/chart/ -f projects/<svc>/deploy/values.yaml
```

`./bootstrap.sh` then `direnv allow` puts `ci`, `helm`, `crane`, `go`,
`python`, `pnpm`, `node` and the formatters on PATH.

- Use `ci`, not bare `bazel` or `bazelisk`: a Mac has no matching remote
  executors. `bb remote` is allowed. Targeted `pytest` on pure-Python files
  you edited is fine as an advisory check.
- **Never pipe `ci` or `bb remote` output into a filter or discard it.** Run
  unpiped or `| tee` to a file. `ci test` has exited 0 without running
  anything (#4118): judge a run by its `Executed N out of M tests` line.
- **BUILD generation is CI-only.** CI's format stage runs `//:gazelle` and
  auto-commits a `style: auto-format` commit, so fetch and rebase before your
  next push.
- Image push happens only in CI on merge to main.

## Knowledge

The knowledge graph (monolith, over MCP) is the shared memory across agents
and sessions, and holds the evidence and incident history behind this repo's
rules. If your tools include it:

- **Search before investigating** (`search_knowledge`) anything that looks
  previously hit: a deploy that will not roll, a red gate, a wedged control
  plane. Results carry `verification_state` and `disputed`: treat them as
  leads to confirm against the repo, not facts.
- **Report durable findings** (`report_knowledge`) with the evidence behind
  them. `dispute_fact` a fact you find wrong; `report_distress` only to request
  intervention.

If you have no MCP tools, this file, the repo and your task spec are all you
have; skip the KG rather than treating it as a blocker. When you write a spec
for another agent, assume it has no KG and put what it must know in the spec.

## Gotchas

- **For `monolith`, `monolith-public` and `embervm`, `targetRevision` in git
  does not say what is deployed.** Kargo owns it on the hub. The
  `projects/gke-apps/` pins are hand-edited floors kept as the revert lever, so
  `git != live` is correct there. Read the live value:
  `kubectl get application monolith -n argocd -o jsonpath='{.spec.sources[0].targetRevision}'`.
  Every other chart deploys off the git value.
- **Never hand-pin `@sha256:` digests for this repo's images in values
  files.** Build-time pinning replaces tags; hand pins go stale into
  `ImagePullBackOff`. Gated. Third-party digest pins are fine.
- **Never hardcode a `.svc.cluster.local` URL.** Helm prepends the release
  name, so a rename silently breaks it. Read it from an env var set in
  `values.yaml` (`envOr("URL", "")`, no default). Gated.
- **Monolith endpoints that read cluster resources need matching `ClusterRole`
  verbs.** A missing verb fails in prod as `Forbidden`, which dashboards show
  as a generic 5xx.
- **Keep bulk data out of `chart/migrations/*.sql`.** The migrations ConfigMap
  has a 256 KiB client-side annotation cap; load seeds out of band
  (`projects/monolith/hikes/seed/`).
- **Grep the tests before changing a number.** TTLs, timeouts, `max_tokens`
  and retry counts are asserted; update the assertions in the same change.
- **Images are apko plus `rules_apko`, never Dockerfiles**, amd64 only (every
  caller passes `arm64 = False`; the macro default is `True`), non-root uid
  65532 with `runAsNonRoot: true`. Re-adding arm64 needs `arm64 = True` and
  per-arch `tars`: `arm64 = False` with `multiarch_tars` fails only at push.
- **Python deps are `@pip//package` via `aspect_rules_py`.** `requirement()`
  does not exist here. JS is pnpm plus `rules_js`. The
  `projects/monolith/frontend/` app is Svelte 5 runes only, with CSS imported
  from JavaScript, never bare `@import` package specifiers inside CSS.
- **Cluster reads** use `kubectl` against the hub context, or the monolith
  MCP's `k8s-*` tools and `verify_deployment`. The ArgoCD UI has no route;
  Kargo's is `private.jomcgi.dev/app/kargo`. MCP topology:
  `projects/mcp/ARCHITECTURE.md`.
- **New service:** a chart and `deploy/` under `projects/<svc>/` (copy
  `projects/monolith/deploy/` for the multi-source pattern), plus a hub
  Application in `projects/gke-apps/<svc>/` listed in its `kustomization.yaml`.
  `ci regen` does not create the hub Application.

## Where to look next

Files under `docs/agents/` are procedures for specific jobs. Open one only
when its row applies.

| When | Read |
|------|------|
| Working under `projects/monolith/` or `projects/embervm/` | that directory's `AGENTS.md` |
| A red CI run you need to diagnose | `docs/agents/ci-triage.md` |
| Opening, readying or enqueuing a PR without GraphQL (cloud session) | `docs/agents/cloud-sessions.md` |
| Reviewing a finished PR diff | `docs/agents/review.md` |
| Taking a feature from idea to merged (`/ship`) | `docs/agents/ship.md` |
| Refreshing a system's STPA model | `docs/agents/stpa.md` |
| Queueing or triaging drain-lane (`qwen-drain`) jobs | `docs/agents/drain-queue.md` |
| Cutting BuildBuddy cache traffic | `docs/agents/buildbuddy-usage.md` |
| Security-sensitive change | `docs/security.md`; `docs/THREAT-MODEL.md` for open findings |
| Public tier: jomcgi.dev, monolith-public, `public_reader` data | `docs/runbooks/public-tier-checklist.md` |
| ArgoCD OutOfSync, stuck rollout, "is my change live?" | `docs/runbooks/argocd-outofsync.md` |
| Adding a service | "New service" above, `projects/platform/ARCHITECTURE.md` section 4, `docs/reference/services.md` |
| Observability or alerting | `docs/observability.md`, `docs/reference/observability-alerting.md` |
| Frontend or design: tokens, palette, motion, a11y | `.impeccable.md` |
| Prose humans read: site copy, READMEs, runbooks, posts | `docs/writing.md` |
| Operator changes | `projects/operators/best-practices.md` |
| How a domain works today | `projects/<domain>/ARCHITECTURE.md`; each `**Why.**` paragraph carries the rationale |
| Build, CI, tooling, hooks, Semgrep | `bazel/ARCHITECTURE.md` |
| Recording a decision | a `**Why.**` paragraph in the domain's ARCHITECTURE.md and a Direction row with its issue; no ADRs (#4667) |

**Runbooks** (`docs/runbooks/`) are explicit-only: open one when Joe names it,
a row above points at it, or a task asks for it. Index:
`docs/runbooks/README.md`.

**Claude Code hooks** (`.claude/settings.json`) duplicate CI and git gates to
fail fast; a rule every author must follow lives in CI or a git hook.

<!-- polylane:start -->
## Investigating production with Polylane

If your tools include the [Polylane MCP server](https://mcp.polylane.com/mcp),
check it for production-behaviour questions (an error, a spike, a deploy, a
missing signal), and read its review comment before merging a PR that touches
production paths.
<!-- polylane:end -->
