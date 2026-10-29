# Argo CD on the hub

The hub Application consumes `values.yaml` and `values-gke.yaml`. Its UI stays
unexposed. Kargo owns the workload chart revisions; the parent `hub` retains
its existing revision ignores and `RespectIgnoreDifferences=true`.

## Controller status ownership

**Why.** Argo CD 3.1.8's `normalizeTargetResources` copies live `status` into
apply targets when respecting ignored differences. A parent sync can then
replay an old child Application operation state after that child completed.
The 2026-10-29 EmberVM incident showed completion at 06:40:14 UTC followed by
the parent's stale `Running` apply at 06:40:17. The deployment verifier must
continue treating that inconsistent record conservatively.

Use the supported vendor 3.3 patch line, explicitly v3.3.18 with the 9.5.11
chart's matching 3.3 CRDs. The image contains upstream
[fix #28201](https://github.com/argoproj/argo-cd/pull/28201), which removes
`status` from both inputs before calculating the ignored-field merge patch.
The chart's default v3.3.9 image is deliberately overridden. Renovate's existing
Helm and image managers own subsequent dependency proposals; no competing
update policy is introduced.

A local backport would change fewer upstream source lines, but this repository
does not build Argo CD. It would introduce a custom shared-controller image,
source and dependency maintenance, provenance and patch responsibility. The
vendor patch release avoids that new build and support surface. It is still a
control-plane upgrade, not a one-line runtime repair.

## Compatibility boundary

- Argo components move together from 3.1.8 to 3.3.18. CRDs gain the upstream
  3.3 schema. The hub's Argo Application already uses server-side apply; the
  chart also annotates CRDs for it, avoiding the larger ApplicationSet CRD's
  client-side annotation limit.
- Chart 9 removes old parameter defaults. The hub explicitly retains its
  prior authentication, namespace, ApplicationSet policy, reconciliation and
  worker settings. No route, principal, credential or Argo RBAC policy is added.
- Upstream narrows ApplicationSet lease RBAC to creation plus get/update of
  its named leader-election lease. No Kubernetes permission is expanded.
- Redis 7.2.11 and Dex 2.44.0 remain pinned. This excludes the chart's unrelated
  Redis 8.2.3 and Dex 2.45.1 upgrades. No new license choice is introduced by
  those dependencies; the vendor Argo code remains Apache-2.0.
- Argo 3.2 adds CronJob health assessment. Existing failed CronJobs can therefore
  affect Application health after upgrade. Do not suppress health checks to
  force a green verdict.
- Argo 3.3 changes Source Hydrator behavior (Git notes and path cleaning).
  Hydration remains disabled here; the incident inventory found no Application
  using `sourceHydrator`. Recheck before deployment.
- The chart adds an optional ApplicationSet profiler ConfigMap mount and
  changes repo-server's binary-copy init command. Existing resource budgets,
  replica counts and deployment selectors are retained. Dex gains an explicit
  non-root UID of 1001 from the vendor chart.

See the upstream [3.1 to 3.2](https://argo-cd.readthedocs.io/en/release-3.3/operator-manual/upgrading/3.1-3.2/)
and [3.2 to 3.3](https://argo-cd.readthedocs.io/en/release-3.3/operator-manual/upgrading/3.2-3.3/)
upgrade notes. Render both overlays and run `upgrade_test` before release.
The regression guards the fixed image on every Argo container, retained auth
and polling behavior, absence of a UI route, CRD apply mode and lease RBAC.

## Release and recovery boundaries

The prepared change is held for shared-controller release approval. Merging
would cause the Git-tracked Argo Application to reconcile and roll its control
plane, independently of first-party chart publication. It does not change
workload chart pins or repair existing child status.

Revert the PR through the protected queue to restore chart 8.6.4 and image
3.1.8. This reintroduces the status-replay defect. Before any downgrade, verify
that no newly introduced CRD fields/features have been adopted; reverting Git
is not a promise of safe schema downgrade. If the controller cannot reconcile
its own rollback, stop for the existing operator recovery procedure. Do not
force-sync, terminate operations or delete annotations as part of this change.

After an approved upgrade, first observe natural parent and child reconciliation.
A corrected parent should stop copying status into new apply configurations.
Old last-applied status can cause a one-time field removal during client-side
apply, so a clean image rollout alone does not prove contaminated children are
repaired. Re-read each child's active operation, history, events and field
ownership. A later normal authorized child sync may establish a fresh terminal
record. If inconsistency persists, keep that recovery separate and reviewed;
never fabricate success or weaken the verifier.
