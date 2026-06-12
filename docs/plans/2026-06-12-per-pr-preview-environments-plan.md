# Per-PR Preview Environments Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Give the monolith an ephemeral preview environment per opted-in open PR, reachable at `<n>.pr.jomcgi.dev`, running the already-built PR image against a copy-on-write clone of production Postgres, with all scheduled and external side effects muted.

**Decision record:** [ADR Platform 005: Per-PR Preview Environments](../decisions/platform/005-per-pr-preview-environments.md). Read it first; this plan implements it.

**Architecture (one paragraph):** An ArgoCD `ApplicationSet` pull-request generator templates one monolith `Application` per open PR carrying the `preview` label. Each Application deploys the monolith image tagged with the PR branch/SHA (already pushed by CI) with `PR_ENV=true`, which skips the scheduler loop and the Discord bot at startup. Each preview also templates a small CNPG `Cluster` that bootstraps copy-on-write from a Longhorn volume snapshot of production `monolith-pg`, so the preview has full prod data with isolated writes and zero up-front copy. The wildcard `*.pr.jomcgi.dev` sits behind one Cloudflare Access policy; Claude Code web and CI reach it via a service token. Previews run in the shared `monolith` namespace, capped at 3 concurrent, and are auto-deleted on PR close.

**Tech stack:** CNPG (volume-snapshot backup + `bootstrap.recovery`), Longhorn CSI snapshots, ArgoCD ApplicationSet, Cloudflare Access service tokens, Gateway API HTTPRoute, the monolith Helm chart, Python/FastAPI startup.

**Non-negotiable house rules (apply to every task):**

- **No em-dashes** in any code, comment, commit, or doc. Use commas, colons, or parentheses.
- **No local test loop.** Write tests, but do NOT run `bazel test`/`pytest`/`vitest` from the workstation. Implement, commit, push, watch CI via `gh pr checks <n> --watch`. Diagnose failures via `mcp__buildbuddy__*`.
- **One code review per PR** at the end, not per task. Implementers self-review before each commit.
- **Conventional Commits** for every commit (one logical step per commit).
- **GitOps only.** No `kubectl apply/patch/edit`. Change Git; ArgoCD syncs.
- **Chart version bumps** update both `chart/Chart.yaml` and `deploy/application.yaml` `targetRevision`.

**Branch:** `claude/per-pr-environments-monolith-fhbdlg`.

**Sequencing:** Phases are ordered so each is independently shippable and verifiable. Phase 1 (mute side effects) has zero prod-deploy risk and unblocks everything. Phases 2-5 can each be a separate PR if preferred; this plan keeps them as tasks under one feature.

---

## Phase 1: Mute side effects with a single `PR_ENV` flag

The two startup points that produce every periodic and external action are the scheduler loop and the Discord bot. Gate both on one flag.

### Task 1.1: Plumb `PR_ENV` into config

**Files:**

- Modify: monolith config/settings module (wherever env is read; same pattern as `DISCORD_BOT_TOKEN`).
- Modify: `projects/monolith/chart/values.yaml` (add `prEnv: false`), `projects/monolith/chart/templates/deployment.yaml` (set `PR_ENV` env from `.Values.prEnv`).

**Steps:**

1. Read `PR_ENV` as a boolean (truthy on `"true"/"1"`), default false, in the same place other env flags are parsed.
2. Surface it as `settings.pr_env` (or the module's idiom) so both the scheduler and bot startup can read one value.
3. Wire `prEnv` through the chart: `values.yaml` default `false`, deployment template emits `- name: PR_ENV` `value: {{ .Values.prEnv | quote }}`.

### Task 1.2: Skip the scheduler loop when `PR_ENV`

**Files:** Modify: `projects/monolith/app/main.py` lifespan startup (the block that launches `run_scheduler_loop`), `projects/monolith/shared/scheduler.py` if a cleaner guard belongs there.

**Steps:**

1. In the lifespan startup, wrap the scheduler-loop launch in `if not settings.pr_env:`.
2. Log a single clear line at startup when skipped: `scheduler loop disabled (PR_ENV)`.
3. Add a unit test asserting the loop task is not created when `PR_ENV=true` (test the startup wiring, do not run the loop).

### Task 1.3: Skip the Discord bot when `PR_ENV`

**Files:** Modify: `projects/monolith/app/main.py:~74` (the `if DISCORD_BOT_TOKEN` bot start).

**Steps:**

1. Change the bot-start condition to also require `not settings.pr_env` (`if discord_token and not settings.pr_env:`).
2. Log `discord bot disabled (PR_ENV)` when skipped.
3. Unit test: bot is not started when `PR_ENV=true` even if a token is present.

**Phase 1 done when:** with `PR_ENV=true`, startup logs both "disabled" lines, no scheduler task is scheduled, and no Discord connection is attempted. Verified on the preview once Phase 4 lands; unit-tested here.

---

## Phase 2: Copy-on-write Postgres clone

### Task 2.1: Enable volume-snapshot backups on production `monolith-pg`

**Files:** Modify: `projects/monolith/chart/templates/cnpg-cluster.yaml`, `projects/monolith/chart/values.yaml`.

**Steps:**

1. Confirm a Longhorn `VolumeSnapshotClass` exists (or add one under `projects/platform/longhorn/`). CNPG needs a CSI snapshot class to target.
2. Add `backup.volumeSnapshot` configuration to the `monolith-pg` Cluster spec referencing that snapshot class, plus a `ScheduledBackup` (or document on-demand snapshot creation) per the ADR's open question on freshness. Default to on-demand at preview create; a periodic schedule is acceptable as a fallback.
3. Keep this gated so production behaviour is unchanged except gaining snapshot capability.

### Task 2.2: Parameterise the chart to template a per-PR CNPG clone

**Files:** Modify: `projects/monolith/chart/templates/cnpg-cluster.yaml` (or a new `cnpg-cluster-preview.yaml`), `projects/monolith/chart/values.yaml`.

**Steps:**

1. Add chart values: `preview.enabled` (bool), `preview.id` (PR number), `preview.snapshotName` (source VolumeSnapshot).
2. When `preview.enabled`, render a `Cluster` named `monolith-pg-pr-<id>` with `instances: 1`, minimal resources, and `bootstrap.recovery` from `volumeSnapshots` pointing at `preview.snapshotName`. Do NOT render the prod cluster in preview mode.
3. Ensure the app `DATABASE_URL` resolves to the clone's `-app` secret (`monolith-pg-pr-<id>-app`) when `preview.enabled`. The deployment template already reads `monolith-pg-app`; make the secret name derive from the cluster name so prod and preview both work.
4. Confirm the Atlas migration init job runs forward-only against the clone (the clone already has prod's applied migrations; only new PR migrations should apply).

**Phase 2 done when:** `helm template` with `preview.enabled=true preview.id=123 preview.snapshotName=...` renders a CNPG clone cluster and a deployment whose `DATABASE_URL` points at the clone's secret, and renders the prod cluster when `preview.enabled=false`.

---

## Phase 3: Wildcard ingress + Cloudflare Access

### Task 3.1: Wildcard DNS and origin cert for `*.pr.jomcgi.dev`

**Files:** Modify: the Cloudflare gateway / tunnel config (wherever `private.jomcgi.dev` is configured), external-dns annotations.

**Steps:**

1. Add a tunnel route / wildcard hostname for `*.pr.jomcgi.dev` to the cloudflared gateway config.
2. Provision a wildcard origin certificate (or confirm the existing cert covers `*.pr.jomcgi.dev`).
3. Add the wildcard CNAME via the external-dns annotation pattern used for the existing hosts.

### Task 3.2: Per-preview HTTPRoute

**Files:** Modify: `projects/monolith/chart/templates/httproute-private.yaml` or add `httproute-preview.yaml`; `projects/monolith/chart/values.yaml`.

**Steps:**

1. When `preview.enabled`, render an HTTPRoute on hostname `<preview.id>.pr.jomcgi.dev` exposing the full (private-tier) route set to the monolith service.
2. Reuse the existing private-route path set (assets, `/api/*`, chat). No public-tier narrowing for previews.

### Task 3.3: Cloudflare Access policy + service token

**Files:** Wherever Cloudflare Access policies are declared for `private.jomcgi.dev` (Access app config / Terraform / dashboard-managed reference).

**Steps:**

1. Add an Access application/policy covering `*.pr.jomcgi.dev` (trusted, same identity as private tier).
2. Create a service token scoped to that policy. Store the client id/secret as a secret (1Password item, same pattern as other monolith secrets) for Claude Code web and CI.
3. Document how to supply `CF-Access-Client-Id`/`CF-Access-Client-Secret` to a preview HTTP client.

**Phase 3 done when:** `<n>.pr.jomcgi.dev` resolves, terminates behind Access, and a request bearing the service-token headers reaches the monolith service.

---

## Phase 4: ApplicationSet PR generator

### Task 4.1: ApplicationSet manifest

**Files:** Create: `projects/monolith/deploy/applicationset-preview.yaml`; modify: `projects/monolith/deploy/kustomization.yaml` to include it; run `format` to regenerate the home-cluster root.

**Steps:**

1. Define an `ApplicationSet` with a `pullRequest.github` generator: owner `jomcgi`, repo `homelab`, `labels: [preview]`, a sane `requeueAfterSeconds`, and token from the existing GitHub credentials secret used by ArgoCD.
2. Template the Application: name `monolith-pr-{{.number}}`, namespace `monolith`, the monolith chart source (same OCI chart + `$values` pattern as `application.yaml`), with Helm value overrides:
   - `prEnv: true`
   - `preview.enabled: true`, `preview.id: {{.number}}`, `preview.snapshotName: <prod snapshot>`
   - image tag `{{.branch}}` (or `{{.head_sha}}`) for backend and frontend
   - hostname `{{.number}}.pr.jomcgi.dev`
   - minimal replicas/resources
3. Add a concurrency guard: cap at 3 previews. If ApplicationSet has no native cap, rely on the `preview` label being the gate and document the cap as a labelling-side limit (enforced in Phase 5).
4. Enable auto-prune/self-heal and `preserveResourcesOnDeletion: false` so closing a PR removes the Application and its CNPG clone.

**Phase 4 done when:** labelling a test PR `preview` causes ArgoCD to create `monolith-pr-<n>`; removing the label or closing the PR deletes it.

---

## Phase 5: CI auto-labelling + concurrency cap

### Task 5.1: Label monolith PRs and enforce the cap

**Files:** Modify: `buildbuddy.yaml` (or the appropriate CI workflow step).

**Steps:**

1. Add a CI step that, on PR events, checks whether the diff touches `projects/monolith/**`.
2. If it does and fewer than 3 previews are currently active, add the `preview` label; otherwise post a single comment that the preview cap is reached and skip labelling.
3. Ensure removing the label (manual opt-out) tears the preview down via the generator.

**Phase 5 done when:** opening a monolith PR auto-applies `preview` (up to the cap) and a non-monolith PR does not get a preview.

---

## End-of-plan verification (on CI, on a real preview)

1. Open a throwaway monolith PR; confirm CI labels it `preview` and ArgoCD creates `monolith-pr-<n>` + `monolith-pg-pr-<n>`.
2. Hit `<n>.pr.jomcgi.dev` with the service token; confirm the app serves and reads cloned prod data.
3. Confirm preview logs show "scheduler loop disabled (PR_ENV)" and "discord bot disabled (PR_ENV)", and that no Discord message was posted and no scheduled job ran.
4. Write to the preview (create a note/task); confirm prod is unaffected.
5. Close the PR; confirm the Application and the CNPG clone are deleted and the pod budget is reclaimed.
6. One comprehensive code review of the full diff before merge.

---

## Out of scope

- App-level primary/replica read-write splitting on prod. The copy-on-write clone is the read/write separation for previews; prod read-routing is a separate HA concern.
- Per-preview public-tier route narrowing. Previews expose the full trusted route set behind Access.
- True storage-disaggregated Postgres branching (Neon-style). Longhorn CoW snapshots cover the need without re-platforming off CNPG.
