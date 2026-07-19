# EmberVM Component Rename Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans (or subagent-driven-development per repo default) to implement this plan task-by-task. This is an infra rename, not a feature: "tests" here are `helm template` renders and post-sync cluster drills, since this repo has no local test loop (push and watch BuildBuddy CI).

**Goal:** Remove the `embervm-embervm-*` name stutter and give every EmberVM component a role-based Kubernetes name, without losing control-plane state or breaking monolith consumers.

**Architecture:** The stutter comes from `embervm.fullname` being `<release>-<chart>` with release and chart both named `embervm`. Fix the helper with the standard Helm `contains` guard so fullname resolves to `embervm`, then rename the three component name-helper families to role names. Workload renames force delete-and-recreate (selector labels are immutable), so the rollout is sequenced across three PRs with transitional alias Services so monolith consumers never see NXDOMAIN.

**Tech Stack:** Helm chart (`projects/embervm/chart/`), ArgoCD GitOps sync, `bazel/tools/git/bump-chart.sh`, BuildBuddy CI.

---

## Naming decision (settled, do not relitigate)

| Object | Today (live) | Target |
| --- | --- | --- |
| Control plane Deployment / SA | `embervm-embervm` | `embervm-control-plane` |
| Control plane API Service | `embervm-embervm` | `embervm` (matches `projects/embervm/docs/api.yaml`, which already documents `http://embervm.embervm.svc.cluster.local:8080`) |
| Node daemon Deployment / Service / SA | `embervm-embervm-noded` | `embervm-node-agent` |
| Node daemon rootfs-builder ConfigMap | `embervm-embervm-noded-rootfs-builder` | `embervm-node-agent-rootfs-builder` |
| Serving Envoy DaemonSet / bootstrap ConfigMap | `embervm-embervm-serving-envoy` | `embervm-serving-relay` |
| Stable serving Service (guest traffic, ports 54xx) | `embervm-embervm-serving` | `embervm-serving` |
| HTTPRoutes / rate-limit policies | `embervm-embervm-serving-<name>` | `embervm-serving-<name>` |
| Release-scoped shared objects (RBAC role, scratch secrets, OnePasswordItems) | `embervm-embervm-*` | `embervm-*` (e.g. `embervm-scratch-k8s`) |
| Op-log PVC | `embervm-embervm-oplog` | **UNCHANGED** (grandfathered, see Task 4) |

Rules:

1. One rule for workloads: every workload, ServiceAccount, and per-component object is `embervm-<role>`. Roles are `control-plane`, `node-agent`, `serving-relay`.
2. The two product-facing Services are the exception: the CP API is plainly `embervm` and the guest serving entry is `embervm-serving`. These are the names external consumers hold.
3. Name the role, not the tech: `serving-relay`, not `serving-envoy` (same reasoning as the repo's no-hardcoded-service-URL rule: implementation detail does not belong in stable identifiers).
4. **Kubernetes-facing names only.** The `noded` binary, the `projects/embervm/noded/` directory, proto service names, and the `noded` gRPC vocabulary do NOT change. This plan touches chart names, service DNS, ServiceAccount principals, and the docs/comments that reference them. Renaming the binary is explicitly out of scope (it would churn the noded fork, protos, and every ADR for zero operational value).

## Hazards the implementer must respect

- **Selector immutability.** `app.kubernetes.io/name` participates in every selector. All three workloads therefore cannot be patched in place; ArgoCD treats each rename as create-new + prune-old. Warm/banked guest VMs on the node die with the noded pod. That is the same blast radius as any chart roll, but do not schedule this mid-drill or while an R7 blessing is in flight.
- **The op-log PVC is the control plane's durable state** (sessions, bank ledger, volume generations; ADR embervm/002). If `{{ include "embervm.fullname" . }}-oplog` re-renders to `embervm-oplog`, ArgoCD creates a fresh empty PVC and prunes the old one. That is state loss. Task 4 pins the live claim name.
- **Consumers hold the old DNS names.** `monolith` (`embervmUrl`, `servingService` x2), `monolith-public` (demo env), and issued scratch kubeconfigs/DSNs all embed `embervm-embervm[-serving]`. The alias Services in Task 5 keep the old names resolving until the consumer PRs land.
- **ServiceAccount principal changes.** `projects/embervm/deploy/values.yaml` allow-lists `system:serviceaccount:embervm:embervm-embervm` (lines 30 and 50). The new SA name lands in the same sync as the principal update (same PR), so there is no auth gap, but a partial cherry-pick of this plan would create one. Ship PR 1 atomically.
- **Every PR whose code must deploy needs its chart bump in the same PR** (`bazel/tools/git/bump-chart.sh`). `deploy/values.yaml` changes are a git `$values` ref and need no bump; `chart/` changes always do.

## Rollout sequence (three PRs, in order)

- **PR 1 (embervm chart):** rename everything inside the embervm chart, add temporary alias Services for the two old external names, pin the op-log PVC name. Cluster converges to new names; old DNS still resolves.
- **PR 2 (consumers):** point monolith, monolith-public, demos, tests, comments, and alerts at the new names.
- **PR 3 (cleanup):** remove the alias Services and their grandfather comments.

Wait for each PR to merge AND for ArgoCD to converge (plus the Task 9 drills after PR 1) before starting the next.

---

### Task 1: Fix the fullname stutter and rename the helper families

**Files:**
- Modify: `projects/embervm/chart/templates/_helpers.tpl` (whole file)

**Step 1: Add the standard guard to `embervm.fullname`**

```gotpl
{{- define "embervm.fullname" -}}
{{- if contains (include "embervm.name" .) .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "embervm.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
```

With release `embervm` this renders `embervm`, so every `{{ include "embervm.fullname" . }}-<suffix>` in the chart de-stutters automatically.

**Step 2: Rename the component helper families**

Rename helpers and their rendered suffixes. Keep the explanatory comments in `_helpers.tpl` (update the names inside them, including the stale "selector is already live and immutable" note, which after this change should say the rename WAS the delete+recreate event):

| Old helper family | New helper family | Rendered name |
| --- | --- | --- |
| `embervm.name` / `embervm.fullname` / `embervm.serviceAccountName` | `embervm.controlPlane.name` / `.fullname` / `.serviceAccountName` for the workload+SA; keep `embervm.name`/`embervm.fullname` as the release-scoped base used by shared objects and the CP API Service | Deployment/SA `embervm-control-plane`, Service `embervm` |
| `embervm.noded.*` | `embervm.nodeAgent.*` | `embervm-node-agent` |
| `embervm.servingEnvoy.*` | `embervm.servingRelay.*` | `embervm-serving-relay` |
| `embervm.serving.fullname` | unchanged name, now renders de-stuttered | `embervm-serving` |
| `embervm.noded.rootfsPath` | `embervm.nodeAgent.rootfsPath` (logic unchanged) | n/a |

Concretely add:

```gotpl
{{- define "embervm.controlPlane.fullname" -}}
{{- printf "%s-control-plane" (include "embervm.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
```

and give the control plane its own `app.kubernetes.io/name: {{ include "embervm.controlPlane.name" . }}` plus `app.kubernetes.io/component: control-plane` selector labels (it may finally gain the component label, since the selector is being recreated anyway).

**Step 3: Render before/after and diff**

```bash
helm template embervm projects/embervm/chart/ -f projects/embervm/deploy/values.yaml \
  | grep -E '^  name:|kind:' > /tmp/after.txt
```

Compare against the same render from origin/main. Expected: every `embervm-embervm` occurrence gone; names match the table above exactly; NOTHING else changed.

**Step 4: Commit**

```bash
git add projects/embervm/chart/templates/_helpers.tpl
git commit -m "refactor(embervm): de-stutter fullname and add role-based name helpers"
```

### Task 2: Update all chart templates to the new helpers

**Files (modify):**
- `projects/embervm/chart/templates/deployment.yaml` (workload name + SA at lines 4/30, noded gRPC addr derivation at line 87, serving stats URL at line 218, oplog claim at line 309: see Task 4)
- `projects/embervm/chart/templates/service.yaml` (CP API Service: `embervm.fullname` so it renders `embervm`)
- `projects/embervm/chart/templates/serviceaccount.yaml`, `rbac.yaml`, `role-monolith-scratch-k8s-secret.yaml`
- `projects/embervm/chart/templates/noded-deployment.yaml`, `noded-service.yaml`, `noded-serviceaccount.yaml`, `noded-rootfs-builder-configmap.yaml`
- `projects/embervm/chart/templates/serving-envoy-daemonset.yaml`, `serving-envoy-configmap.yaml` (bootstrap ConfigMap name, Envoy `cluster:` id at line 29, xDS address at line 99 which must render `embervm.embervm.svc.cluster.local`)
- `projects/embervm/chart/templates/serving-service.yaml`, `serving-httproute.yaml`
- `projects/embervm/chart/templates/onepassworditem-scratch-k8s.yaml`, `onepassworditem-scratch-postgres.yaml`, `workload-scratch-k8s.yaml`, `workload-scratch-postgres.yaml`, `workload-demo-postgres.yaml` (scratch secret names become `embervm-scratch-*`)
- Rename files: `noded-*.yaml` to `node-agent-*.yaml`, `serving-envoy-*.yaml` to `serving-relay-*.yaml` (template filenames are cosmetic to Helm but should match the objects they render)

**Step 1: Mechanically swap helper includes per the Task 1 table.** No logic changes. The `EMBERVM_NODED_ADDR`-style env derivation in `deployment.yaml:87` must now render `embervm-node-agent.embervm.svc.cluster.local:<port>`.

**Step 2: Render and diff again** (same command as Task 1 Step 3). Expected names, exhaustively: `embervm` (Service), `embervm-control-plane` (Deployment, SA), `embervm-node-agent` (Deployment, Service, SA), `embervm-node-agent-rootfs-builder`, `embervm-serving-relay` (DaemonSet), `embervm-serving-relay-bootstrap`, `embervm-serving`, `embervm-serving-<workload>` HTTPRoutes, `embervm-scratch-k8s`, `embervm-scratch-postgres`, `embervm-scratch-k8s-secret-reader`, RBAC `embervm`.

**Step 3: Commit**

```bash
git add projects/embervm/chart/templates/
git commit -m "refactor(embervm): role-based component names in all templates"
```

### Task 3: Update the embervm deploy values (principals + comments)

**Files:**
- Modify: `projects/embervm/deploy/values.yaml:24-30` and `:50`

**Step 1:** Replace both `system:serviceaccount:embervm:embervm-embervm` principals with `system:serviceaccount:embervm:embervm-control-plane`, and rewrite the surrounding comment (it explains the stutter; explain the role name instead: `kubectl create token embervm-control-plane`).

**Step 2: Commit** (`fix(embervm): allow-list the renamed control-plane service account`).

### Task 4: Grandfather the op-log PVC name

**Files:**
- Modify: `projects/embervm/chart/templates/pvc.yaml:5`, `projects/embervm/chart/templates/deployment.yaml:309`
- Modify: `projects/embervm/chart/values.yaml` (add `opLog.claimName: ""`)
- Modify: `projects/embervm/deploy/values.yaml` (set `opLog.claimName: embervm-embervm-oplog`)

**Step 1:** Make the claim name overridable, defaulting to the derived name:

```gotpl
{{- $oplogClaim := default (printf "%s-oplog" (include "embervm.fullname" .)) .Values.opLog.claimName -}}
```

Use `$oplogClaim` in both the PVC template and the Deployment's `claimName`.

**Step 2:** In `deploy/values.yaml`, pin `opLog.claimName: embervm-embervm-oplog` with a comment: grandfathered pre-rename claim holding the live op-log (sessions, bank ledger, volume generations); renaming it means a fresh empty PVC and total CP state loss; migrate only via an explicit copy drill, never a rename.

**Step 3:** Render with and without the override; confirm the pinned name appears in both PVC and Deployment, and that the default path still renders `embervm-oplog` for fresh installs.

**Step 4: Commit** (`fix(embervm): pin grandfathered oplog PVC name through the rename`).

### Task 5: Transitional alias Services for the two old external names

**Files:**
- Create: `projects/embervm/chart/templates/legacy-alias-services.yaml`

**Step 1:** Add two plain ClusterIP Services, `embervm-embervm` (CP API ports, selector = new control-plane selector labels) and `embervm-embervm-serving` (serving ports, selector = the serving-relay selector, copied from `serving-service.yaml`). Gate both behind `.Values.legacyNameAliases.enabled` (default `true` in `chart/values.yaml`), and comment that PR 3 of the rename plan deletes this file. This keeps `embervmUrl`, issued scratch DSNs/kubeconfigs, and monolith-public demo env resolving during the consumer transition.

**Step 2:** Render; confirm both alias Services carry the same ports as the Services they shadow.

**Step 3: Commit** (`feat(embervm): temporary alias services for pre-rename DNS names`).

### Task 6: Bump the embervm chart, open PR 1, converge, drill

**Step 1:** `bazel/tools/git/bump-chart.sh projects/embervm`

**Step 2:** Push, open PR 1 (`refactor(embervm): role-based component names (rename plan PR 1/3)`), watch `gh pr checks --watch`, merge with `--rebase` when green.

**Step 3:** After ArgoCD sync, verify with kubectl (reads only): pods `embervm-control-plane-*`, `embervm-node-agent-*`, `embervm-serving-relay-*` Running; Services `embervm`, `embervm-serving`, plus both aliases present; old workloads pruned; `kubectl get pvc -n embervm` still shows `embervm-embervm-oplog` Bound.

**Step 4:** Run the Task 9 drills before starting PR 2.

### Task 7: Update consumers (PR 2)

**Files:**
- Modify: `projects/monolith/deploy/values.yaml:171` (`embervmUrl` to `http://embervm.embervm.svc.cluster.local:8080`)
- Modify: `projects/monolith/chart/values.yaml:828-831` and `:859-861` (`servingService: embervm-serving`, rewrite both comments that currently explain the stutter)
- Modify: `projects/monolith-public/chart/values.yaml:254` and `:268` (CP URL and scratch DSN host)
- Modify: `projects/monolith/demos/k8s_terminal_api.py:65` (`EMBER_K8S_ENTRY_HOST` default) and `:72` (`EMBER_K8S_SECRET_NAME` default to `embervm-scratch-k8s`)
- Modify: `projects/monolith/sandbox/scratch_k8s_test.py:27,108` (test fixtures use the new serving host)
- Modify: `projects/firecracker/sandbox/guest-init/internal/handler/kernel.go:249,253` (comments: `kubectl logs deploy/embervm-node-agent`)
- Modify: `projects/platform/signoz-addons/alerts/templates/embervm-oplog-pvc-usage.yaml:37` (description: name the PVC as the grandfathered `embervm-embervm-oplog`, noting the rename plan kept it)
- Modify: `projects/embervm/runtimes/k3s/drill/scratch-k8s-manual-wake-drill.md` (drill commands use new names)

**Step 1:** Grep for any straggler the inventory missed before editing:

```bash
grep -rn "embervm-embervm\|embervm-noded\|serving-envoy" projects/ docs/ \
  --include='*.yaml' --include='*.py' --include='*.go' --include='*.ex' --include='*.md' \
  | grep -v docs/plans/ | grep -v docs/decisions/
```

Historical plans and ADRs are grandfathered records: do NOT churn them. Also sweep SigNoz alert templates and any dashboards for queries filtering on old deployment/pod names.

**Step 2:** Apply the edits. Config values that tests assert on: update `scratch_k8s_test.py` in the same commit as the values change (per the CI-diagnosis rule in CLAUDE.md).

**Step 3:** Chart bumps in the same PR: `bazel/tools/git/bump-chart.sh projects/monolith` and `bazel/tools/git/bump-chart.sh projects/monolith-public` (both charts changed; the monolith deploy values need no bump but ride along). The signoz-addons chart also changed: bump it too if it is versioned the same way (check its deploy/application.yaml).

**Step 4:** Push, open PR 2 (`refactor(monolith): consume role-based embervm names (rename plan PR 2/3)`), watch CI, rebase-merge, verify ArgoCD convergence and that scratch demos still connect (Task 9 drills 2 and 3 again).

### Task 8: Remove the aliases (PR 3)

**Files:**
- Delete: `projects/embervm/chart/templates/legacy-alias-services.yaml`
- Modify: `projects/embervm/chart/values.yaml` (drop `legacyNameAliases`)

**Step 1:** Confirm nothing still resolves the old names before deleting:

```bash
grep -rn "embervm-embervm" projects/ --include='*.yaml' --include='*.py' --include='*.go' --include='*.ex' | grep -v docs/plans/
```

Expected: only the grandfathered oplog PVC pin and its comments.

**Step 2:** Delete the file, drop the value, `bazel/tools/git/bump-chart.sh projects/embervm`, PR 3 (`chore(embervm): drop pre-rename alias services (rename plan PR 3/3)`), rebase-merge on green.

### Task 9: Post-rollout verification drills

Run after PR 1 (all three), and re-run drills 2 and 3 after PR 2:

1. **CP state survived:** `kubectl logs deploy/embervm-control-plane -n embervm` shows op-log replay from the grandfathered PVC (no fresh-database init lines); banked sessions from before the roll are still listed by the CP API.
2. **Serving path:** the scratch-postgres demo connects via port 5401 and the scratch-k8s kubeconfig via 5410 (during the alias window the old host also still resolves: `kubectl run` a curl pod or use the monolith demo page).
3. **CP API:** `embervmUrl` health endpoint returns healthy from a monolith pod (old URL during alias window, new URL after PR 2).
4. **xDS relay:** `embervm-serving-relay` pods are Ready and their bootstrap points at `embervm.embervm.svc.cluster.local` (render check plus pod env/config inspection).

## Explicitly out of scope

- Renaming the `noded` binary, `projects/embervm/noded/` directory, protos, or gRPC service names.
- Rewriting historical plans, ADRs, or drill logs that mention old names (grandfathered).
- Migrating the op-log PVC to the new name (possible later as a dedicated copy drill; not worth the risk now).
- Any behavior change. Every PR here must render to a pure-rename diff.
