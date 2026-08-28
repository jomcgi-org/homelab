# ArgoCD OutOfSync / Stuck Deploy Runbook

A decision tree for diagnosing ArgoCD apps that are OutOfSync, stuck mid-sync, or Degraded when nothing looks wrong. These are the failure modes that have actually shown up in this cluster, in the order to check them.

## Step 0: is the app actually progressing?

Always start here:

```bash
kubectl -n argocd get application <app> -o yaml
```

Look at `status.sync.status`, `status.operationState` (phase, message, startedAt), and `status.health.status`. Everything below is a diagnosis for a specific pattern in that output.

## Stale chart-render cache

**Symptom:** a chart TEMPLATE change (not just a values change) deployed, but the live pod is missing a newly added env var, volume, or similar, even though the OCI chart digest and the git values are correct.

**Cause:** ArgoCD's repo-server can serve a stale cached render of the chart.

**Fix:** force a fresh sync via the `monolith-k8s-sync-argocd-app` MCP tool (name=`<app>`), or a hard refresh. Note (third documented occurrence, 2026-07-04): the MCP sync can report `synced: true` while changing nothing; the reliable escalation is a hard-refresh annotation, which forces the repo-server to re-pull and re-render:

```bash
kubectl --context local-homelab -n argocd annotate application <app> argocd.argoproj.io/refresh=hard --overwrite
```

This has recurred at least three times: whenever git values and the OCI chart digest both look correct but the live manifest disagrees, suspect this first before digging further.

## Multi-app PR rollout ordering

**Symptom:** a PR that changes two apps (e.g. fc-invoke enabling auth + monolith sending the token) produces a window of real errors (401s) between the two rollouts, because ArgoCD rolls the apps at different speeds.

**Fix (prevention):** for a hard cutover, split into two PRs and ship the caller/compat side first, then the enforcing side. Post-merge errors during a known two-app rollout window are expected; verify both apps converged before debugging.

## Same-version chart republish wedge

**Symptom:** `sync=OutOfSync` and `operationState=Succeeded` coexist forever, and won't clear no matter how many times selfHeal runs.

**Cause:** a code merge without a chart version bump republishes the SAME chart version with new image tags. ArgoCD's last sync operation succeeded against what it pulled at the time, but the pull is mutated underneath it, so the diff never converges. This is largely prevented since the idempotent-publish change in `bazel/helm/push.sh.tpl`, but can still surface if a merge slips through without a version bump.

**Fix:** since ADR platform/009 decision 1 there is no bump PR to open, because PRs carry no chart version. The version is computed and published on main and then committed back by `chart-version-bot`. If a merge has not deployed, check whether that write-back commit landed: `git log --author=chart-version-bot -3 origin/main`. If main CI's publish went green but the write-back did not, the chart is in the registry and main simply does not reference it yet, so re-run the action rather than opening a bump PR. Another sync is not the fix in either case.

## Deploy killed by the workflow wall clock before the publish

**Symptom:** a merged commit never deploys, and its `deploy` invocation reports `Failed` after running for almost exactly one hour. Every `image.push` child invocation is green; there is no chart publish output after them and no `chart-version-bot` write-back commit (`git log --author=chart-version-bot -3 origin/main` shows nothing for the merge).

**Cause:** the third documented way a merge can end up deployed-but-for-nothing or not-deployed-at-all (alongside the same-version republish wedge above and the ruleset-refused write-back in `write-back-versions.sh`): the deploy action hit BuildBuddy's default workflow wall clock BEFORE reaching the publish stage. The ordering runs the expensive, retryable work first (image builds) and the cheap, must-not-be-skipped work last (chart publish plus write-back), so a wide apko rebuild that fills the budget loses exactly the part that cannot be lost. Any change invalidating many apko images at once has this shape: a Renovate lock refresh, a base image bump.

First seen on 1e63fee7 (invocation 06feb469): #4862 refreshed 14 apko locks, all 14 images rebuilt cold, the pushes spanned ~52 green minutes, and the action was killed at 3600s with the publish never started.

**Fix:** re-run the deploy (an empty `ci:` retrigger commit or a re-run from the BuildBuddy UI). Every stage is idempotent: `push-changed.sh` skips content-identical images, `push.sh.tpl` skips versions already published, and the next clean run computes and writes back. If a kill lands MID-publish instead of before it, the result is charts in the registry with no write-back; check whether the highest published `charts/*` version outruns main's `Chart.yaml` and simply re-run to converge.

`deploy` now sets an explicit `timeout: "2h"` in `buildbuddy.yaml`, sized for a 14-image cold rebuild plus publish, so recurrence needs a wider rebuild than that. If runs STILL die at almost exactly one hour, the cap is BuildBuddy-side (plan tier, or a remote_runner_default_timeout experiment clamping explicit requests), not the YAML field, and only BuildBuddy can lift it.

## Duplicate env var name in a container

**Symptom:** permanent phantom OutOfSync that never converges, even after repeated syncs.

**Cause:** the chart renders N env entries for a container, but two of them share the same name. The Kubernetes API server collapses duplicate env names on apply, last occurrence wins but keeps the first occurrence's position, so live reads back fewer entries than the chart renders. The diff is real but structural, not a transient sync issue.

**Diagnose:** render the exact OCI chart version with `helm template` and diff it against `kubectl get deploy -o json`, stripping server-added defaults and whatever the app's `ignoreDifferences` already excludes.

**Fix:** remove the duplicate in the chart. A CI check in `ci-diff-manifests.sh` now catches this at PR time, so it should not reach prod again, but old duplicates already deployed still need a manual chart fix.

## Phantom OutOfSync from ServerSideApply and an undeclared schema default

**Symptom:** an app is `OutOfSync` but `Healthy`, nothing in the cluster is wrong, and selfHeal re-syncs forever without converging. The offending resource differs only by a field nobody wrote, typically one a CRD schema defaults.

**Cause:** `ServerSideApply=true` changes what the diff means. Client-side apply does a 3-way merge against `last-applied-configuration` and treats a field that is in live but absent from both desired and last-applied as somebody else's, so server defaults are invisible. SSA diffs on field OWNERSHIP instead: a field the chart never declared is missing from the predicted state while the apiserver has defaulted it into live, and that gap can never close.

Seen on all five SSA apps at once (2026-08-14): Gateway API defaulting `parentRefs.group/kind` and `backendRefs.group/kind/weight` on HTTPRoutes, and Envoy Gateway defaulting `remoteJWKS.cacheDuration: 300s` on SecurityPolicies. The tell is that the identical live objects under non-SSA apps (longhorn and monolith) stayed Synced.

**Diagnose:** the sync status alone will not say which field. Pull ArgoCD's own view and diff the two states it computed:

```bash
bazel/tools/cluster/scripts/argocd-api.sh /api/v1/applications/<app>/managed-resources
```

Each item carries `normalizedLiveState` and `predictedLiveState`. Diff those two per item. There is no `.diff` field on the item, so a filter like `select(.diff != "")` matches nothing and reports every app clean.

**Fix:** DECLARE the defaulted field in the chart, so ArgoCD owns it and the prediction matches live. Prefer that to ignoring the field. An ignore also hides real changes to it, and the list for this class naturally includes `backendRefs[].weight`, which is live traffic shaping: a deliberate weight change would be silently dropped while the app still reported Synced. Same lesson as `/spec/replicas` in `projects/platform/argocd/values.yaml`.

Ignore only when the manifest comes from an upstream chart you cannot edit. Kyverno's CRDs are the standing example: the chart emits `annotations: {}` and `labels: {}`, the apiserver drops empty maps on write, and `{} != absent`, so a normalizer deletes the map only when it is already empty.

## ignoreDifferences at the Application level

An earlier note here said `spec.ignoreDifferences` on the Application is inert, observed on v3.1.6. That is NOT true on v3.1.8: an app-level block on the `argocd` Application demonstrably suppressed the HTTPRoute default drift while `kargo`, which lacked one, drifted on the identical fields. Verify against the running version before relying on either behaviour.

Note that suppressing the diff is not the same as not applying the value; that still needs `RespectIgnoreDifferences=true`.

**Historic note (v3.1.6).** The global normalizer is still the right home for anything that should apply fleet-wide, and is the only option for a resource no single Application owns: `projects/platform/argocd/values.yaml`, under `resource.customizations.ignoreDifferences`. This was proven by checking `/api/v1/applications/<app>/managed-resources` and confirming `normalizedLiveState` was unaffected by an app-level ignore but respected a global one. See [ArgoCD API access without the UI](#argocd-api-access-without-the-ui) below for how to query that endpoint.

## Stuck sync operation

**Symptom:** an operation has been running (or "hung") far longer than it should, and selfHeal is not recovering it.

**Cause:** selfHeal does not recover a wedged operation; it only re-triggers a new sync, which can wedge again the same way.

**Fix:** terminate the stuck operation directly:

```bash
kubectl -n argocd patch application <app> --type merge -p '{"status":{"operationState":{"phase":"Terminating"}}}'
```

The Application CRD has no status subresource, so a plain merge patch on `status` is correct here, this is not a workaround. It takes 20-40 seconds to take effect: do not conclude the patch failed if nothing has changed after 5 seconds.

## Fleet-wide freeze via the root app

`canada` is the root app-of-apps: it syncs `projects/home-cluster` at HEAD. If `canada` itself is wedged, nothing downstream deploys, including merged chart bumps that look fine in git.

Two known wedge modes:

- **Stuck PreSync hook Job**: a hook Job blocks the operation indefinitely.
- **Dangling kustomize reference**: a file was deleted but is still listed in a `kustomization.yaml`. This puts `canada` into `ComparisonError` with `sync=Unknown`.

**Diagnose:**

```bash
kubectl -n argocd get application canada -o jsonpath='{range .status.conditions[*]}{.type}: {.message}{"\n"}{end}'
```

**Fix:** correct the git issue (remove the stuck hook or the dangling reference), then force `canada` to re-read via `monolith-k8s-sync-argocd-app`.

## App is Degraded but no resource in the tree looks degraded

**Cause:** this app has `status.resourceHealthSource: appTree`, which means per-resource health is not populated in `status.resources[]`. You have to walk the resource tree by hand to find the actual unhealthy resource.

**Example root cause seen in practice:** a `OnePasswordItem` was `Ready=False` because the underlying vault item was never created. Nothing about that showed up as a top-level Degraded resource until the tree was walked.

## "Is my fix live?"

The question that matters is "which commit is this chart version built from", not "did a newer chart deploy". A chart bump that merged just before yours can roll out first and look like your fix landed when it didn't.

```bash
git log origin/main -- <svc>/chart/Chart.yaml
```

Match the chart version currently synced against the version history here, and confirm your commit is actually included in that version, not just that some newer version deployed.

## ArgoCD API access without the UI

There is no ArgoCD MCP server. For anything not covered by `kubectl get applications`, use:

```bash
bazel/tools/cluster/scripts/argocd-api.sh <api-path> [curl-args...]
# e.g.
bazel/tools/cluster/scripts/argocd-api.sh /api/v1/applications/monolith/managed-resources
```

It handles the port-forward, reads the admin password from `argocd-initial-admin-secret`, exchanges it for a session JWT, and calls the path with that token. Two things that bite if you hand-roll this instead:

- The API is served at a SUBPATH and over plain HTTP. `server.rootpath=/app/argocd` and `server.insecure=true` (both in `argocd-cmd-params-cm`) mean `/api/v1/...` is a 404 and an `https://` request to the forwarded port never handshakes. The script reads both from the cluster rather than hardcoding them. Getting either wrong fails as an EMPTY response, not an error, which reads as "this app has no diff".
- The session POST needs `-H 'Content-Type: application/json'`, otherwise the API returns 415.
- Admin credentials come from the `argocd-initial-admin-secret` secret in the `argocd` namespace, base64-decoded.

## CI flakes (not an ArgoCD issue, but shows up in the same investigations)

**BES sidecar flake:** BuildBuddy's BES sidecar occasionally produces `invalid-peer` WRN log lines and exit code 38 that look like a test failure but are not.

**Before retriggering:** verify there is no genuine test failure first, quote the actual log line, do not assume flakiness. Once confirmed:

```bash
git commit --allow-empty -m "ci: retrigger after BES sidecar flake"
git push
```

**Docker Hub anonymous-pull 429s:** CI handles this with an authenticated `docker login` using BuildBuddy secrets. If you see a 429 locally instead, authenticate your own Docker Hub session rather than treating it as a CI bug.
