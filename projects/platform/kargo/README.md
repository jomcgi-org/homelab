# Kargo

Owns **one thing**: the dev environment's chart version.

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

**The cost, stated plainly:** dev's `targetRevision` stops being git truth. It
lives in the live Application.

## Required manual step

The `canada` root Application owns `monolith-dev` from git with `selfHeal`, so
it will revert every promotion unless told to ignore that one field. `canada` is
a bootstrap resource and is **not in this repo**, so this is a one-time patch:

```sh
kubectl patch application canada -n argocd --type=merge -p '{
  "spec": {
    "ignoreDifferences": [
      {
        "group": "argoproj.io",
        "kind": "Application",
        "name": "monolith-dev",
        "jsonPointers": ["/spec/sources/0/targetRevision"]
      }
    ]
  }
}'
```

This is the same pattern the monolith Application already uses to let the HPA
own `/spec/replicas`: ArgoCD owns the resource, one field belongs to another
controller.

**Without this patch Kargo appears to work and nothing changes**: the promotion
succeeds, `canada` reverts the field on its next sync, and the only symptom is
dev staying on its old chart.

## What is deliberately off

| Setting | Why |
| --- | --- |
| `api.enabled: false` | No UI or CLI for v1. It needs TLS, an ingress and an auth decision; the controller reconciles without it |
| `controller.rollouts.integrationEnabled: false` | Rollouts is only for Kargo's *verification* features. Promotion needs none, and disabling explicitly grants fewer permissions |
| `controller.argocd.integrationEnabled: true` | **Required.** Without it the controller does not watch Applications and argocd-update fails |

## Verifying it works

Do not trust "the Promotion succeeded". Check the field:

```sh
kubectl get application monolith-dev -n argocd \
  -o jsonpath='{.spec.sources[0].targetRevision}{"\n"}'
kubectl get stage dev -n kargo-monolith -o jsonpath='{.status}{"\n"}'
kubectl get freight -n kargo-monolith
```

The `targetRevision` must match the newest published chart, and must still match
it a few minutes later. If it reverts, the `canada` patch above is missing.
