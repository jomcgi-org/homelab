# Agent reference

Task-specific details for `AGENTS.md`. Read the section needed for the work.
Root merge authority applies throughout these procedures.

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
- **Generated sources needed by ordinary tooling belong in a clean checkout.**
  Keep pinned Bazel generation authoritative, seed only the required outputs
  as tracked files, and verify their bytes against regenerated outputs in
  Linux CI. Reuse the trusted branch autoformat writeback with `git add -u`,
  after parallel formatters finish; never nest Bazel in `run-generators.sh`.
  Keep main and merge-queue no-write guards. Do not check in other build
  artifacts. The EmberVM Go protobuf targets are the pattern (#7066).
- Image push happens only in CI on merge to main.

## Gotchas

- **For `monolith`, `monolith-agents`, `monolith-public`, `embervm` and
  `scotland-connector`, `targetRevision` in git does not say what is deployed.** Kargo owns it on the
  hub. The `projects/gke-apps/` pins are hand-edited floors kept as the revert lever, so
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
- **Images are apko plus `rules_apko`, never Dockerfiles**, amd64 only (all
  callers pass `arm64 = False`; the macro default is `True`),
  non-root uid 65532 with `runAsNonRoot: true`. Re-adding arm64 needs
  `arm64 = True` and per-arch `tars`: `arm64 = False` with `multiarch_tars`
  fails only at push.
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

## Human handoff

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

## Knowledge and production evidence

Use `search_knowledge` for recurring failures; confirm results against current
code and live state. Report durable findings with evidence and dispute facts
shown wrong. Unavailable MCP tools are not a blocker; briefs for other agents
must include the evidence they need.

For production behaviour, use Polylane when available and read its review
comment before landing production-path changes. Follow
`docs/runbooks/argocd-outofsync.md` for rollout failures.

## Where to look next

Files under `docs/agents/` are procedures for specific jobs. Open one only
when its row applies.

| When | Read |
|------|------|
| Working under `projects/monolith/` or `projects/embervm/` | that directory's `AGENTS.md` |
| A red CI run you need to diagnose | `docs/agents/ci-triage.md` |
| Opening, readying or enqueuing a PR without GraphQL (cloud session) | `docs/agents/cloud-sessions.md` |
| Reviewing a finished PR diff | `docs/agents/review.md` |
| Shipping a feature | `docs/agents/ship.md` |
| Refreshing a system's STPA model | `docs/agents/stpa.md` |
| Queueing or triaging drain-lane (`qwen-drain`) jobs | `docs/agents/drain-queue.md` |
| Cutting BuildBuddy cache traffic | `docs/agents/buildbuddy-usage.md` |
| Security-sensitive change | `docs/security.md`; `docs/THREAT-MODEL.md` for open findings |
| Public tier: jomcgi.dev, monolith-public, `public_reader` data | `docs/runbooks/public-tier-checklist.md` |
| ArgoCD OutOfSync, stuck rollout, "is my change live?" | `docs/runbooks/argocd-outofsync.md` |
| Adding a service | "New service" in Gotchas above, `projects/platform/ARCHITECTURE.md` section 4, `projects/gke-apps/kustomization.yaml` (the live workload list) |
| Observability or alerting | `docs/observability.md` |
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
