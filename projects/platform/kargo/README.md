# Kargo

Owns **one thing**: the monolith's chart version, in dev and then in production.

The pipeline is `dev` then `prod`. Both run the same chart, so production can
only receive Freight that dev has already taken. That ordering is the only gate
today, and it proves a chart deployed rather than that it works. Synthetic
verification is the follow-up; see "What is deliberately off".

`projects/monolith/dev/deploy/application.yaml` pinned an OCI chart version that
nothing moved. `bazel/helm/write-back-versions.sh` resolves exactly one
`deploy/application.yaml` per chart and that is production's, so dev was
invisible to it. The stale pin failed twice in one night (#4728): once leaving a
safety gate inert, once withholding a feature.

## Why promotion patches the Application instead of committing to git

ADR platform/009 decision 3 specified a git-writing promotion. That path is
blocked, not merely inconvenient.

Ruleset `9180009` on the default branch carries `required_status_checks`, and
its only bypass actor is `RepositoryRole 5` (admin). A push to main from a
non-admin identity is refused, so the git path needs an **admin-scoped token
living in the cluster**. This repo's own guidance is never to make CI a ruleset
bypass actor, and an admin PAT for a dev-only promotion is a far larger surface
than the problem it solves.

`argocd-update` needs no credential: it is a Kubernetes RBAC permission the
controller already holds. The charts are public, so the Warehouse needs no
registry credential either. ADR 009 called the git write credential "the main
new surface"; this design removes it.

**The cost, stated plainly:** `targetRevision` stops being git truth for both
environments. It lives in the live Application. Dev's file has read `0.293.0`
since the first promotion while dev ran `0.295.1`, and production's will drift
the same way once the write-back is eventually retired. "What version is
production on" becomes a `kubectl` question, which matters more for production
than for dev because it gets asked during incidents.

## Required manual step

The `canada` root Application owns both monolith Applications from git with
`selfHeal`, so it will revert every promotion unless told both to ignore that
field and to leave it alone when applying. `canada` is a bootstrap resource and
is **not in this repo**, so this is a manual patch:

```sh
kubectl patch application canada -n argocd --type=merge -p '{
  "spec": {
    "syncPolicy": {
      "syncOptions": ["RespectIgnoreDifferences=true"]
    },
    "ignoreDifferences": [
      {
        "group": "argoproj.io",
        "kind": "Application",
        "name": "monolith-dev",
        "jsonPointers": ["/spec/sources/0/targetRevision"]
      },
      {
        "group": "argoproj.io",
        "kind": "Application",
        "name": "monolith",
        "jsonPointers": ["/spec/sources/0/targetRevision"]
      }
    ]
  }
}'
```

**`RespectIgnoreDifferences=true` is not optional, and leaving it out fails in
a way that looks like success for hours.** `ignoreDifferences` governs
DIFFING: it stops ArgoCD reporting the app OutOfSync over that field, so
`selfHeal` is never triggered *by* it. It does nothing about APPLYING. When a
sync runs for any other reason, ArgoCD applies the whole rendered manifest and
stamps the git value straight back.

That is exactly what happened to dev. The promotion held for nine hours and
several verifications, because nothing had triggered a `canada` sync yet. The
next merge that touched a file `canada` renders caused one, and dev went from
`0.295.1` back to `0.293.0` in a single reconcile with nothing reporting an
error.

So "the promotion stuck, and was still stuck a few minutes later" is NOT
sufficient evidence. The check that matters is that it survives a `canada`
sync, which you can force by merging anything it renders, or observe via
`.status.operationState.finishedAt` moving on `canada`.

The repo already uses this option for the same reason in
`projects/platform/coredns/application.yaml` and
`projects/platform/kyverno/application.yaml`: ArgoCD owns the resource, another
controller owns one field.

**Send the whole list every time.** A `--type=merge` patch REPLACES an array
rather than appending to it, so patching in the production entry alone deletes
the dev one, and the only symptom is dev quietly going back to reverting its
own promotions. There is no error.

**This patch IS the production cutover**, not the paperwork around it. The
moment `monolith`'s `targetRevision` is ignored, production's deploys depend
entirely on Kargo: ArgoCD stops acting on the value the CI write-back
maintains. Removing the `monolith` entry is therefore the revert, and it is
immediate, because the write-back has kept that value correct all along.

**Not** the same as the HPA case, despite looking like it, and the difference
is what makes this easy to get wrong. The monolith Application ignores
`/spec/replicas` so the HPA can own it, and that works with no sync option
because the chart's Deployment **omits** `spec.replicas` entirely when
autoscaling is on. There is no value in the rendered manifest, so a sync has
nothing to stamp back. `targetRevision` is always present, so it always is.

**Without the entry for an Application, Kargo appears to work and nothing
changes**: the promotion succeeds, `canada` reverts the field on its next sync,
and the only symptom is that environment staying on its old chart.

## What is deliberately off

| Setting | Why |
| --- | --- |
| `api.enabled: false` | No UI or CLI for v1. It needs TLS, an ingress and an auth decision; the controller reconciles without it |
| `controller.rollouts.integrationEnabled: false` | Rollouts is only for Kargo's *verification* features, and Rollouts is not installed. **This is what stops `prod` having a real gate**, so it is the thing to change when synthetic checks arrive |
| `controller.argocd.integrationEnabled: true` | **Required.** Without it the controller does not watch Applications and argocd-update fails |

No `verification` block on either Stage, therefore. A Stage with nothing to
verify reports `Freight has been verified`, which reads like a gate passed and
is really a gate absent.

## Why the Warehouse spells out its defaults

`interval`, `freightCreationPolicy` and `discoveryLimit` are CRD server-side
defaults, and the chart writes them anyway. A field the chart omits and the API
server sets is a diff ArgoCD can never close, so the Warehouse was permanently
OutOfSync from the first install.

That is not cosmetic. `selfHeal` syncs only the **drifted** resources, so an
unconvergeable resource emits a continuous stream of targeted single-resource
syncs, and each one preempts the full sync:

```
syncId=14233  Sync/1 kargo.akuity.io/Stage:kargo-monolith/prod  nil->obj   <- planned
syncId=14241  (7s later) sync resources filter: [Warehouse/monolith-chart] <- replaced it
```

`Stage/prod` was planned and never applied, and the Application reported
`Succeeded` throughout, because the Warehouse-only sync genuinely did succeed.
One resource that cannot converge starves every other resource in the same
Application behind a green status.

Worth generalising when adding any CRD to a chart: check the live object for
fields the API server added, and either write them or accept a permanent diff.
`kubectl get <kind> <name> -o jsonpath='{.spec}'` against a freshly applied
object is the whole check.

## Verifying it works

Do not trust "the Promotion succeeded", and do not trust the Stage's verified
message either. Check the field, in both environments:

```sh
for app in monolith-dev monolith; do
  printf '%-14s ' "$app"
  kubectl get application "$app" -n argocd \
    -o jsonpath='{.spec.sources[0].targetRevision}{"\n"}'
done
kubectl get stage -n kargo-monolith
kubectl get promotion -n kargo-monolith
```

Each `targetRevision` must match the newest published chart, and must still
match it a few minutes later. If it reverts, that Application's `canada` entry
is missing, or `RespectIgnoreDifferences=true` is not set.

The end-to-end assertion is stronger than either number: after a chart
publishes, `dev` should move first and `prod` second, and the production pod
should restart onto it. A version that moves while no pod restarts means the
render was identical, which is a real outcome and not a failure.
