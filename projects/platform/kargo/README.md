# Kargo

Kargo owns the live chart revision of five Applications on the GKE hub.
The UI is at `private.jomcgi.dev/app/kargo`, behind Cloudflare Access and
Authentik. The `homelab-admin` group has promotion rights.

## Hub pipelines

The hub's `kargo` Application in `projects/platform-gke/kargo/application.yaml`
loads this chart with `values.yaml` only. There is no additional values overlay
in that Application. `promotion.enabled` and every Stage's `autoPromotion`
are true in this configuration.

| Pipeline | Project / namespace | Warehouse | Stage: Application |
| --- | --- | --- | --- |
| monolith | kargo-monolith | monolith-chart | prod: monolith |
| monolith-agents | kargo-monolith-agents | monolith-agents-chart | prod: monolith-agents |
| monolith-public | kargo-monolith-public | monolith-public-chart | prod: monolith-public |
| embervm | kargo-embervm | embervm-chart | dev: embervm-dev; prod: embervm |

All five Applications live in `argocd` and are registered through
`projects/gke-apps/kustomization.yaml`. The three monolith pipelines subscribe
directly to their Warehouses and have no upstream Stage or soak. There is no
registered monolith dev Application. This configuration makes no decision
about adding one.

Each Warehouse polls its OCI chart under `ghcr.io/jomcgi/homelab/charts` every
five minutes, subject to its configured semver constraint and discovery limit.
The chart name matches the pipeline name. Discovered versions become Freight.
`ProjectConfig` enables auto-promotion separately for each Stage.

Registry access is configured per pipeline. `monolith-agents` sets
`credentialItemPath` to the managed 1Password item
`vaults/k8s-homelab/items/argocd-ghcr-repo-creds`. The template creates
`OnePasswordItem/monolith-agents-chart-creds` in `kargo-monolith-agents` with
`kargo.akuity.io/cred-type: helm`. The operator supplies the Secret; Kargo
matches its `repoURL`, `username` and `password` fields to the chart repository.
The other pipelines do not configure this item. No credential needs to be read
or changed to maintain these docs.

## What promotion verifies

Every Stage runs `argocd-update`, then `argocd-wait`. The first updates the
matching chart source's `targetRevision` and waits for its sync operation.
The second waits for sync, operation and health together: Synced, no sync in
flight, and Healthy. The per-pipeline timeout is ten minutes for the monolith
pipelines and ninety minutes for EmberVM.

These checks establish point-in-time Kubernetes readiness. They do not prove
functional correctness or HTTP reachability from the controller to a workload.
`argocd-wait` also has no `desiredRevision`: an ArgoCD status refresh can briefly
report pre-apply health. A successful Promotion alone is insufficient evidence
of complete deployment verification.

EmberVM dev adds a functional check after `argocd-wait`. The `conformance` HTTP
step polls the dev runner's verdict endpoint and accepts a pass only when
`chart_version` matches the Freight being promoted. Missing or stale verdicts
keep polling. Two consecutive failed cycles for that chart fail the step;
the configured verdict timeout is seventy-five minutes. This gate is separate
from `promotion.revisionGate`.

EmberVM prod requests Freight from `dev`, with `requiredSoakTime: 5m0s`.
Normally, Freight must be verified in dev and held there for five minutes
before prod can take it. The soak provides upstream exposure time; it does
not replace the conformance verdict or guarantee the absence of later faults.
Argo Rollouts integration and Kargo's separate `verification` block are unused.
The assertions live in promotion steps, and a Stage without a `verification`
block marks Freight verified when its Promotion succeeds.

### Revision gate remains disabled

`promotion.revisionGate.enabled` is false in the effective hub configuration.
Only monolith's prod Stage carries `revisionURL`; no `rollout-version` step is
rendered while the flag is false. `promotion_gate_test.py` checks that this
wiring is inert and that enabling it leaves EmberVM's conformance steps intact.

When enabled, a Stage with `revisionURL` polls `/healthz` for a `chart_version`
equal to the Freight's version. Stale or missing fields keep polling until the
timeout. Before enabling it through Git, the workload must ship the field and
an operator must confirm the running endpoint reports the expected version.
The flag does not add functional tests. This README correction does not enable
it or claim that operational prerequisite has been verified (#4745).

## Keep promotions across root syncs

`hub` is the root Application, defined in
`projects/gke-cluster/root/root-application.yaml`. Its parent, `hub-root`, syncs
that directory; the parent definition is
`projects/gke-cluster/hub-root-application.yaml`. Root ownership changes belong
in Git and deploy through that parent.

The root's `ignoreDifferences` list covers `monolith`, `monolith-agents`,
`monolith-public`, `embervm` and `embervm-dev`, each at
`/spec/sources/0/targetRevision`. It is paired with
`RespectIgnoreDifferences=true`. Diff suppression alone hides drift; the sync
option also prevents a later apply from stamping the Git pin over a promotion.

Kargo uses the controller's Kubernetes RBAC to update Applications, without a
Git write credential. Live `targetRevision` is Kargo-owned. Pins under
`projects/gke-apps/` are bootstrap/revert floors, so a Git pin is not evidence
of the deployed version. Returning revision ownership to Git requires an
explicit Git-based handoff with the intended pin and promotion policy reviewed
together.

There is a source-array caveat: the root manifest records that these JSON
pointers combined with `RespectIgnoreDifferences` can leave other
`spec.sources` fields unchanged, including `repoURL`, chart names and value-file
paths, while ArgoCD reports success. After source changes, an operator should
compare live Applications with the rendered Git definitions using
`bazel/tools/ci/check-application-drift.sh` against the hub. See
`docs/runbooks/argocd-outofsync.md` for related root and sync diagnostics.

When arming another pipeline through Git:

1. Register its Application with the correct chart source and
   `kargo.akuity.io/authorized-stage` annotation. Each Stage must match that
   source exactly.
2. Add the root's revision ignore entry and retain
   `RespectIgnoreDifferences=true`. Let `hub-root` reconcile this change before
   arming promotion, and confirm the live root has it.
3. Enable the pipeline only after its Application, ownership rules and any
   upstream or functional prerequisites are in place. Check source drift and
   confirm the promoted revision survives subsequent root reconciliation.

The Warehouse explicitly renders `interval`, `freightCreationPolicy` and
`discoveryLimit` to match CRD defaults. Omitting server-defaulted fields can
leave permanent drift and repeated targeted syncs that starve other resources.

## Approval, retry and deployment are separate

Freight approval in the Kargo UI is the operator lever for a hotfix or blocked
upstream. Approval for a target Stage bypasses upstream verification and its
soak requirement. It authorizes that Freight for promotion; it does not prove
the chart passed those checks or that deployment succeeded.

Kargo does not automatically retry failed or errored Promotions. An operator
can request a new promotion after resolving the cause, using Freight already
eligible for that Stage. The `kargo_promote` tool checks eligibility and refuses
to override gates or overlap a running/queued Promotion. It does not approve
Freight. Inspect the failure before retrying, then verify the resulting rollout.

## Read-only verification

Set `HUB_CONTEXT` to your kubeconfig context for `homelab-hub`. These examples
inspect hub objects and do not approve Freight, promote, sync or patch anything:

```sh
kubectl --context "${HUB_CONTEXT:?Set the homelab-hub context}" -n argocd get application hub -o yaml
for project in kargo-monolith kargo-monolith-agents kargo-monolith-public kargo-embervm; do
  kubectl --context "$HUB_CONTEXT" -n "$project" get warehouses,stages,promotions,freights
done
for app in monolith monolith-agents monolith-public embervm-dev embervm; do
  kubectl --context "$HUB_CONTEXT" -n argocd get application "$app" \
    -o jsonpath='{.metadata.name}{" "}{.spec.sources[0].targetRevision}{" "}{.status.sync.status}{" "}{.status.health.status}{"\n"}'
done
```

Compare the live revision with the intended eligible Freight and inspect its
Promotion and Stage status. EmberVM dev must satisfy its chart-aware verdict
before normal prod eligibility and the soak; explicit approval is a separate
bypass to record. Do not require every Application to equal the newest
published chart while discovery, upstream checks or rollout are in progress.

Use `verify_deployment` with the Application name and intended chart version
as `expected_revision` for the rollout verdict. It checks sync, health,
operation, errors, live revision and Kargo state. Poll an `in_progress` verdict.
If checking manually, require Synced and Healthy, verify the workload's expected
image and rollout, and confirm the service answers. A chart change that renders
identical pod templates need not restart pods. Confirm revision ownership
survives root reconciliation before concluding that a reverting promotion is
resolved.
