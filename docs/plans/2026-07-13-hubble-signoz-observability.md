# Hubble Drop-Alert + Scrape + Topology Decommission Plan

> **For Claude:** This plan is **all Track 1** (Git/PR changes), executable via
> `superpowers:subagent-driven-development`. No maintenance window, no node
> operation. Each task is edit + render/verify + commit, deferring test execution
> to end-of-plan CI on the pushed branch (no local test loop in this repo). It
> implements capability **#1** of ADR
> [networking/003](../decisions/networking/003-cilium-capability-adoption.md) and
> the topology-decommission cleanup.

**Goal:** Scrape Hubble metrics into SigNoz and alert on a sustained policy-denied
drop rate, so a misconfigured egress gate surfaces in seconds. Then decommission
the orphaned `/api/home/observability/topology` endpoint and its rollup job (the
service-map DAG it fed was retired in `a58c5b1a1`).

**Scope note:** The L7 HTTP golden-signal alerts (error-rate / p99 latency) are
**not** in this plan. They require an L7 `CiliumNetworkPolicy` on each monitored
surface to make Hubble emit `httpV2` series at all, so they live with that policy
work in `2026-07-13-cilium-policy-hardening.md`. This plan builds the shared
`hubble-metrics` scrape job those alerts will later read from, but ships only the
drop alert, which needs the already-emitted `drop` metric and no datapath change.

**Tech Stack:** Cilium 1.19.5 (Hubble), SigNoz (k8s-infra otel-collector
prometheus receiver), SigNoz alert ConfigMaps, ClickHouse `signoz_metrics`,
monolith (FastAPI) for the decommission.

---

## Decisions locked for this plan

- **Only the drop alert ships here.** HTTP alerts are deferred to the policy plan
  because they cannot fire without an L7 policy emitting `httpV2` series.
- **The scrape job is shared infra.** It picks up `hubble_drop_total` now and will
  pick up `hubble_httpv2_*` automatically once the policy plan adds L7 surfaces.
- **Alert routes to `incidentio`**, matching the existing alert ConfigMaps.

---

## Task 1: Add a SigNoz scrape job for hubble-metrics

**Files:**
- Modify: `projects/platform/signoz/values-prod.yaml` (the
  `otelDeployment.config.receivers` block and the metrics pipeline that lists the
  prometheus receivers, ~lines 59-165)

**Step 1:** Add a `prometheus/hubble` receiver modeled on the removed
`prometheus/linkerd-proxy` job (git-recover it from commit `18fe56e28` for the
exact relabel style). The `hubble-metrics` endpoint is exposed by the
`cilium-agent` pods on port 9965:

```yaml
        prometheus/hubble:
          config:
            scrape_configs:
              - job_name: "hubble"
                scrape_interval: 15s
                kubernetes_sd_configs:
                  - role: pod
                    namespaces:
                      names: ["kube-system"]
                relabel_configs:
                  - source_labels: [__meta_kubernetes_pod_label_k8s_app]
                    action: keep
                    regex: cilium
                  - source_labels: [__meta_kubernetes_pod_phase]
                    action: keep
                    regex: Running
                  - source_labels: [__meta_kubernetes_pod_ip]
                    target_label: __address__
                    replacement: "$1:9965"
                  - source_labels: [__meta_kubernetes_namespace]
                    target_label: k8s_namespace_name
                  - source_labels: [__meta_kubernetes_pod_name]
                    target_label: k8s_pod_name
```

**Step 2:** Add `prometheus/hubble` to the metrics pipeline `receivers` list
(the same pipeline the old `prometheus/linkerd-proxy` was removed from).

> **NOTE:** verify the Hubble metrics port on the running chart before finalizing:
> `kubectl get svc -n kube-system hubble-metrics -o yaml` (default 9965) and the
> agent pod's exposed ports. Scraping the agent pods directly (role: pod, keep
> k8s-app=cilium) gives node-local flow metrics; the `hubble-metrics` headless
> Service is an alternative SD target.

**Step 3:** Bump the signoz chart if its Application pins a version. Run
`grep -nE 'targetRevision|chart:|repoURL' projects/platform/signoz/application.yaml`
- pinned version → `bazel/tools/git/bump-chart.sh projects/platform/signoz`
- rendered from a git path → values change syncs directly, no bump

**Step 4:** `bazel/tools/format/fast-format.sh`; commit
`feat(signoz): scrape Hubble metrics (drop alert now, L7 later)`.

**Rollback:** revert the receiver + pipeline additions.

---

## Task 2: Build the policy-drop alert

**Files:**
- Create: `projects/platform/signoz-addons/alerts/templates/hubble-policy-drops.yaml`

**Step 1:** Model on
`projects/platform/signoz-addons/alerts/templates/pod-restart-rate.yaml`. A
builder-query alert over `hubble_drop_total` filtered to the **policy-denied** drop
reason (verify the exact reason label value via
`kubectl exec -n kube-system ds/cilium -- hubble observe --verdict DROPPED`, this is
typically `reason="POLICY_DENIED"`), firing when the policy-drop rate is sustained
above zero over the eval window. Keep it scoped to policy drops so benign
non-policy drops do not page. Channel `incidentio`; label `category: network`.

> **NOTE:** this is the egress-gate safety net. It depends only on the `drop`
> metric already in `hubble.metrics.enabled` and Task 1's scrape, nothing L7.

**Step 2:** Bump signoz-addons chart if its Application pins a version. `format`;
commit `feat(signoz): Hubble policy-drop alert`.

**Rollback:** delete the template file.

---

## Task 3: Decommission the orphaned topology endpoint and rollup

**Files:**
- Delete: `projects/monolith/home/observability/topology_config.py`,
  `topology_query.py`, and their tests (`topology_config_test.py`,
  `rollup_test.py` topology cases, `router_test.py` topology cases)
- Modify: `projects/monolith/home/observability/rollup.py` (remove
  `topology_rollup`, `_write_topology_snapshot`, the `build_topology` import, and
  the scheduled-job registration)
- Modify: `projects/monolith/home/__init__.py` (drop the topology route from the
  public read-only home routes)
- Modify: the observability router that serves `/api/home/observability/topology`
  (remove the route)
- Modify: `projects/monolith/home/tests/bdd_api_test.py` (remove the
  `@covers_route("/api/home/observability/topology")` case)

**Step 1 (pre-check, load-bearing):** confirm nothing outside the frontend calls
the endpoint before deleting:

Run: `grep -rniE "observability/topology|build_topology|topology_rollup" projects/monolith projects/monolith-public --include=*.py --include=*.svelte --include=*.ts | grep -v _test`
Expected: only the definitions above; no external/MCP/public-site caller. If a
caller exists, stop and surface it.

**Step 2:** Remove the code and the scheduled-job registration. Per
[scheduler-jobs-ops], a DB-backed scheduler leaves an **orphan job row** when a
handler is deleted; note in the PR that the `topology_rollup` row must be removed
from the jobs table out-of-band (or via the scheduler CLI) after deploy, or it will
log handler-not-found each tick.

**Step 3:** This is monolith application code, so the image rebuilds and the
monolith chart needs a bump: `bazel/tools/git/bump-chart.sh projects/monolith`.
Removing public callables can trip `bdd_completeness_test` (per
[bdd-completeness-public-surface]); the route removal must be matched by the
`covers_route` removal in the same commit.

**Step 4:** `format`; render-check
(`helm template monolith projects/monolith/chart -f projects/monolith/deploy/values.yaml >/dev/null`);
commit `refactor(monolith): remove orphaned topology endpoint and rollup (DAG retired in a58c5b1a1)`.

**Rollback:** revert the commit; the endpoint and rollup return.

---

## Verification (end-of-plan, on the pushed branch)

1. **CI green** on the branch (format + `bazel test //...`). The monolith test
   removals and chart bumps are the CI-visible risk; watch via
   `gh pr checks <n> --watch` and read failures with `mcp__buildbuddy__*`.
2. **After merge + sync**, confirm the scrape works: check SigNoz for
   `hubble_drop_total` series with the drop-reason and workload labels present.
3. **Confirm the drop alert is registered** in SigNoz and quiet against the current
   baseline. Do not intentionally break egress to test it.
4. **Confirm the topology endpoint is gone:** `curl -s .../api/home/observability/topology`
   returns 404, and the scheduler no longer logs a `topology_rollup` handler.

## References

| Resource | Relevance |
| -------- | --------- |
| ADR networking/003 | Capability #1 + the decommission this plan implements |
| commit `18fe56e28` | The removed `prometheus/linkerd-proxy` scrape job (relabel template) |
| commit `a58c5b1a1` | Retired the service-map DAG (why the topology backend is orphaned) |
| `projects/platform/signoz-addons/alerts/templates/pod-restart-rate.yaml` | Alert ConfigMap format to model on |
| `docs/plans/2026-07-13-cilium-policy-hardening.md` | Where the L7 golden-signal alerts live (they need an L7 policy first) |
