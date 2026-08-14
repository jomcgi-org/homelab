# Kargo

Owns **one thing**: the monolith's chart version, in dev and then in production.

The pipeline is `dev` then `prod`. Both run the same chart, so production can
only receive Freight that dev has already taken. That ordering is joined by two
assertions, both promotion steps rather than a Kargo `verification` block: an
`argocd-wait` that holds each Promotion until its Application is Synced and
Healthy, and a 2 minute `requiredSoakTime` before prod may take dev's Freight.
A Promotion that fails never marks its Freight verified, which is the mechanism
that stops a bad chart at dev. See "What the gate does and does not assert".

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

The `canada` root Application owns every promoted Application from git with
`selfHeal`, so it will revert every promotion unless told both to ignore that
field and to leave it alone when applying. `canada` is a bootstrap resource and
is **not in this repo**, so this is a manual patch.

**This patch is `--type=merge`, and a merge patch REPLACES the whole
`ignoreDifferences` array.** So it must always carry an entry for EVERY
Application any pipeline promotes, all at once. Re-applying an older copy of
this block that lists fewer Applications silently drops the rest, and the
symptom is the one described below: promotions that hold for hours and then
revert on an unrelated merge, with nothing reporting an error. When a pipeline
is added to `promotion.pipelines` in `values.yaml`, its Applications must be
added here in the same change.

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
      },
      {
        "group": "argoproj.io",
        "kind": "Application",
        "name": "embervm-dev",
        "jsonPointers": ["/spec/sources/0/targetRevision"]
      },
      {
        "group": "argoproj.io",
        "kind": "Application",
        "name": "embervm",
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
| `controller.rollouts.integrationEnabled: false` | Rollouts is only for Kargo's `verification` block, which is why the gate is built from promotion steps instead. Turning it on is still the path to revision-aware analysis (ADR platform/009 option A) |
| `controller.argocd.integrationEnabled: true` | **Required.** Without it the controller does not watch Applications, and both argocd-update and argocd-wait fail |
| `api.oidc.dex.enabled: false` | authentik does PKCE, so Kargo talks to it directly. Dex would be a second identity hop and a second thing to upgrade |
| `api.adminAccount.enabled: false` | authentik is the identity provider; a local password would be one more credential to hold and rotate |

No `verification` block on either Stage, therefore. That matters for how you
read the UI: a Stage with nothing to verify reports `Freight has been verified`
the moment its Promotion succeeds, so that status reflects the Promotion, not
the gate. Read the Promotion's step results instead.

## What the gate does and does not assert

**Asserts.** The promoted chart reached the environment, the sync operation
finished, and the workload came back Healthy. For a Deployment, ArgoCD reports
Healthy only once `observedGeneration` has caught up and the new pods pass their
probes, and the monolith's readiness probe is `/healthz`. So waiting for Healthy
is the assertion that the promoted chart's health check went green. Production
additionally requires dev to have held the Freight for 2 minutes, which catches
a rollout that comes up Healthy and then crashloops but is explicitly not long
enough to catch a slower-onset regression.

**Does not assert.** Anything functional beyond the readiness probe. The obvious
next step, an `http` step against dev's deep `/api/health`, is blocked twice
over and both are worth knowing before anyone tries it:

1. `monolith-dev`'s `/api/health` returns **503 today and always has**, because
   the `stars` component fails closed on an empty `stars.sites` table and dev
   has no stars data. The ember components fail *open* ("no probe recorded
   yet"), so they are not the problem. Gating on that endpoint would freeze
   production permanently.
2. The `monolith-dev-api-ingress` CiliumNetworkPolicy allowlists
   `envoy-gateway-system`, `mcp`, `monolith-workflows` jobs, the whatsapp
   sidecar and `embervm`. The `kargo` namespace is **not** on it, so an `http`
   step would silently dial-timeout rather than fail honestly. That policy
   exists to limit who can send the `X-Auth-Email` identity header, so adding
   Kargo is an auth-surface decision, not a config tweak.

There is also a residual race: `argocd-wait` has no `desiredRevision` field, so
it asserts against whatever target is current. `argocd-update` waits for its
sync operation first, so the applied manifests are the promoted chart's, but
ArgoCD's status refresh can still briefly report the pre-apply Healthy. #4745
tracks closing this properly.

**If a Promotion wedges.** A failed Promotion never verifies its Freight, so the
chart is stranded upstream. The lever is Freight approval in the UI, which
supersedes both upstream verification and the soak time.

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
