# AGENTS.md

Instructions for every agent working in this repo, whatever the tool or
model. Claude Code reads this through `.claude/CLAUDE.md`; other agents read it
directly. Which model does which work is decided by the factory, not here.

A Kubernetes homelab at
[jomcgi-org/homelab](https://github.com/jomcgi-org/homelab). The GKE hub
(`homelab-hub`) has served every workload since 2026-08-31 and is the sole
management plane. The home k3s fleet was dropped on 2026-09-18
([#4964](https://github.com/jomcgi-org/homelab/issues/4964)); its checked-in
configuration is residual, and no issue authorizes tearing it down.
`projects/platform/ARCHITECTURE.md` has the shape. Services, operators and
websites live under `projects/<name>/`, each colocating its Helm `chart/` with
the `deploy/` config ArgoCD ships it from. Everything builds with Bazel (bzlmod)
and deploys from Git. Go, Python, JavaScript and Starlark.

Use `ls` for structure and `git log` for history. The rest of this file is what
you would only learn by breaking it. Rules marked **(gated)** are enforced by
CI or a git hook, so a violation fails loudly rather than silently.

## Invariants

- **The cluster is GitOps, so `kubectl` is read-only.** Never `apply`, `patch`,
  `edit`, `scale`, `label` or `delete`, and never `helm install` or
  `helm uninstall`. To change something, edit
  `projects/<service>/deploy/values.yaml`, commit, push, and ArgoCD syncs it.
- **Never commit to main (gated: `protect-main.sh`).** Worktree, branch, PR.
  The repo allows rebase merges only, through the GitHub merge queue
  (`gh pr merge --auto --rebase` enqueues). Never rebase a PR only because main
  moved: the queue does that. See the `pr-workflow` skill.
- **Never touch a chart's `version:` or pinned `targetRevision:` (gated:
  `bazel/tools/ci/chart_version_guard.py`).** Main's publish computes the next
  version after merge and `chart-version-bot` writes both lines back (ADR
  platform/009), so a deploy lands one commit after the merge, not in it.
- **`ci` is the feedback loop, not PR CI.** Run it before pushing.
- **Conventional Commits (gated: `commit-msg` hook).**
- **No em-dashes in anything you write**: prose, comments, docs, commit
  messages, PR bodies. Use a comma, colon, parentheses, or split the sentence.
  Existing ones are grandfathered; do not churn files to strip them.
- **GitHub Issues are the source of truth for outstanding work**, not committed
  plan files. `docs/plans/` is retired. The domain's `ARCHITECTURE.md` records
  a decision and its rationale; the issue records what is left to do.
- **Secrets come from the 1Password Operator** (`OnePasswordItem` CRD). Never
  hardcode one. Nothing is exposed to the internet directly: all traffic goes
  through Cloudflare.

## How to work

- Any task that modifies tracked files runs in a dedicated git worktree on a
  new branch, and ends as a pushed PR: ready for review when validation passes,
  draft when it does not or a question is open. Report the PR URL and state.
  If you cannot open the PR, stop and report the blocker; never leave an
  uncommitted implementation or unrelated changes on the branch.
- Reach for the simplest approach that holds. On a genuine design fork, put
  two or three options in front of Joe ranked by complexity with a
  recommendation, then wait. Skip that for config fixes, renames, and anything
  already scoped.
- Completion claims are verified, not trusted: check the artifact (`git show
  --stat`, re-read the file) before building on or reporting another agent's
  work. A change that must deploy is done only when the rollout is verified
  live; `pr-workflow` has the checklist.
- When debugging, state the hypothesis and run the one command that would
  falsify it before writing any fix.
- For site copy and CV prose, audit facts and flag unsupportable claims, but
  offer at most one draft: Joe writes the final wording.
- Blocked on a decision only Joe can make while he may be away: send one
  `monolith-monolith-agent-notify` line, if you have the MCP tools. Subagents report
  blockers to their dispatcher instead.

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
  executors and the results mislead. `bb remote` is allowed; `ci` is better.
  Targeted `pytest` on hermetic, pure-Python files you edited is fine as an
  advisory check.
- **Never pipe `ci` or `bb remote` output into a filter or discard it.** Run
  unpiped or `| tee` to a file. `ci test` has exited 0 without running
  anything (#4118): judge a run by its `Executed N out of M tests` line.
- **BUILD generation is CI-only.** CI's format stage runs `//:gazelle` and
  auto-commits, so a BUILD change arrives as a `style: auto-format` commit and
  your next push needs a fetch and rebase first.
- Image push happens only in CI on merge to main.

## Knowledge

The knowledge graph (monolith, over MCP) is the shared memory across agents
and sessions. If your tools include it:

- **Search before investigating** (`search_knowledge`) anything that looks
  previously hit: a deploy that will not roll, a red gate, a wedged control
  plane. Results carry `verification_state` and `disputed`: treat them as
  leads to confirm against the repo, not facts.
- **Report durable findings** (`report_knowledge`) with the evidence behind
  them. `dispute_fact` a fact you find wrong; `report_distress` only to request
  intervention.

If you have no MCP tools, this file, the repo and your task spec are all you
have; skip the KG rather than treating it as a blocker.

## Gotchas

- **For `monolith`, `monolith-public` and `embervm`, `targetRevision` in git
  does not say what is deployed.** Kargo owns it on the hub. The
  `projects/gke-apps/` pins are hand-edited floors kept as the revert lever, so
  `git != live` is correct there. Read the live value:
  `kubectl get application monolith -n argocd -o jsonpath='{.spec.sources[0].targetRevision}'`.
- **Never hand-pin `@sha256:` image digests in values files.** Build-time
  pinning replaces tags; hand-pinned digests go stale into `ImagePullBackOff`.
  Nothing in CI catches one.
- **Never hardcode a `.svc.cluster.local` URL.** Helm prepends the release
  name, so a rename silently breaks it. Read it from an env var set in
  `values.yaml` (`envOr("URL", "")`, no default). Nothing in CI catches one
  either.
- **Monolith endpoints that read cluster resources need matching `ClusterRole`
  verbs.** A missing verb fails in prod as `Forbidden`, which dashboards show
  as a generic 5xx.
- **Keep bulk data out of `chart/migrations/*.sql`.** The migrations ConfigMap
  is applied client-side with a 256 KiB annotation cap; a seed breaks sync.
  Seeds load out of band (`projects/monolith/hikes/seed/`).
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
- **There are no kubernetes or ArgoCD MCP servers.** Use `kubectl` against the
  hub context. The ArgoCD UI has no route; Kargo's is at
  `private.jomcgi.dev/app/kargo`. MCP topology: `projects/mcp/ARCHITECTURE.md`.
- **New service:** copy a recent `deploy/` directory (`projects/monolith/deploy/`)
  for the multi-source pattern, adjust names, then `ci regen`.

## Test-writing traps

- Nearly every subpackage under `projects/monolith/` is gazelle-excluded
  (`grep gazelle:exclude projects/monolith/BUILD`): a new `*_test.py` there
  needs a hand-written `py_test` in `projects/monolith/BUILD` or it never runs.
- SQLite in tests: a file-backed database under `tmp_path`. An in-memory
  StaticPool database is one connection and deadlocks concurrency tests.
- SQLite returns naive datetimes where Postgres is tz-aware: assert
  `isinstance(value, datetime)`, and coerce before comparing.
- `build_app()` calls `logging.basicConfig(force=True)`, which removes
  pytest's caplog handler: re-add it after `build_app`.
- Mock async callables with async functions; a sync lambda fails only at
  runtime.
- Assert on ORM objects inside the session context; afterwards attribute
  access lazy-loads and throws.
- Never monkeypatch a builtin through a module attribute (`module.open`);
  patch `builtins.open` or restructure the seam.

## EmberVM invariants

- Base snapshots clone guest process memory, so a restored guest is
  bit-identical to the base. Restore-time triggers must derive from external
  state the restore changed (device superblock, mount table), never from
  in-process state.
- The control plane's kv logger renders only whitelisted `@meta_keys`; a new
  structured log field must be added to the whitelist or it is dropped.

## Where to look next

| When | Read |
|------|------|
| Security-sensitive change | `docs/security.md`; `docs/THREAT-MODEL.md` for open findings |
| Public tier: jomcgi.dev, monolith-public, `public_reader` data | `docs/runbooks/public-tier-checklist.md` |
| ArgoCD OutOfSync, stuck rollout, "is my change live?" | `docs/runbooks/argocd-outofsync.md` |
| Adding a service | `docs/contributing.md`, `docs/reference/services.md` |
| Observability or alerting | `docs/observability.md`, `docs/reference/observability-alerting.md` |
| Frontend or design: tokens, palette, motion, a11y | `.impeccable.md` |
| Prose humans read: site copy, READMEs, runbooks, posts | `docs/writing.md` |
| Operator changes | `projects/operators/best-practices.md` |
| How a domain works today | `projects/<domain>/ARCHITECTURE.md`; each `**Why.**` paragraph carries the rationale |
| Build, CI, tooling, hooks, Semgrep | `bazel/ARCHITECTURE.md` |
| Recording a decision | a `**Why.**` paragraph in the domain's ARCHITECTURE.md and a Direction row with its issue; no ADRs (#4667) |

**Skills** (`.claude/skills/`, plain markdown procedures any agent can
follow): `pr-workflow`, `ci-triage`, `ship`, `stpa`,
`improve-buildbuddy-usage`, `qwen-queue`.

**Runbooks** (`docs/runbooks/`) are explicit-only: open one when Joe names it,
a row above points at it, or a task asks for it. Index:
`docs/runbooks/README.md`.

<!-- polylane:start -->
## Investigating production with Polylane

[Polylane](https://polylane.com/?ref=github.onboarding-pr) is an AI production engineer: it watches deploys, telemetry, incidents, and the infrastructure this repository ships to, investigates problems as they happen, and proposes fixes as pull requests. It is connected to this repository and available to coding agents through the [Polylane MCP server](https://mcp.polylane.com/mcp).

- When a question involves production behaviour (an error, a spike, a deploy, a missing signal), query Polylane through its MCP tools before reasoning from the code alone.
- When debugging a failure, start from the incident or issue Polylane recorded: it carries the evidence an investigation already gathered.
- Polylane reviews pull requests in this repository against the live infrastructure. Read its review comment before merging changes that touch production paths.
<!-- polylane:end -->
