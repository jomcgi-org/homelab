# Cilium L7 Policy Hardening + Observability Plan (per-surface L7 → default-deny)

> **For Claude:** This plan has two parts. **Part A (#2, L7 HTTP policies + the
> golden-signal alerts they unlock)** is incremental Track-1 Git/PR work,
> executable via `superpowers:subagent-driven-development`, one sensitive surface
> at a time. **Part B (#4, `policyEnforcementMode: always`)** is a **gated**
> change: its tasks must not run until the coverage audit (Task B1) is complete and
> signed off, because flipping to default-deny before coverage is proven denies
> every unselected workload. It implements capabilities **#2 and #4** of ADR
> [networking/003](../decisions/networking/003-cilium-capability-adoption.md).

**Goal:** Add L7 HTTP (method/path) enforcement to the sensitive surfaces where it
adds real authorization, harvesting the per-service error-rate and p99-latency
alerts that each L7 policy unlocks as a byproduct. Then, once L3/L4 policy coverage
is audited complete, flip cluster policy to default-deny.

**Why enforcement and observability are one task:** Cilium emits `httpV2` metrics
only for traffic an L7 `CiliumNetworkPolicy` has redirected to its per-node Envoy
(Cilium 1.19 has no annotation-only visibility path). So applying an L7 policy to a
surface is what *both* enforces method/path *and* makes Hubble produce that
surface's HTTP metrics. Each Part-A task therefore ships a policy and its alerts
together.

**Architecture:** Per ADR networking/003. The load-bearing correctness rules
(documented for DNS in `monolith-public/chart/templates/cilium-policy.yaml:115-122`):
(1) Cilium resolves overlapping rules toward the more permissive L4, so **an L4
allow covering the same port silently shadows an L7 HTTP rule** (killing both
enforcement and visibility); (2) an L7 `http` block is an **allow-list**, so
restricting a multi-path service to one path breaks its other routes. L7 parsing
also adds an Envoy hop (latency) to matched flows, which is why this is per-surface.

**Tech Stack:** Cilium 1.19.5 `CiliumNetworkPolicy` (L7 HTTP), Hubble `httpV2`
metrics, SigNoz alert ConfigMaps, Helm charts for monolith-public,
firecracker/substrate, and platform/cilium.

---

## PART A: L7 HTTP policies + harvested alerts (#2, incremental)

### Task A0: Enable the httpV2 Hubble metric (inert until a surface has an L7 policy)

**Files:**
- Modify: `projects/platform/cilium/values.yaml` (the `hubble.metrics.enabled` list, ~lines 40-46)
- Modify: `projects/platform/cilium/bootstrap/cilium-helmchart.yaml` (the mirrored list, ~lines 61-66)

**Step 1:** In both files (they MUST stay identical, or a node reboot reverts the
metric set), add a narrowly-scoped `httpV2` entry:

```yaml
    metrics:
      enabled:
        - dns
        - drop
        - tcp
        - flow
        - "httpV2:sourceContext=workload;destinationContext=workload;labelsContext=source_namespace,destination_namespace"
```

> **NOTE:** `httpV2` (not the deprecated `http`) folds HTTP status onto
> `hubble_httpv2_requests_total`, which the error-rate alert needs; v1 `http` keeps
> status on a separate `hubble_http_responses_total`. The context options make the
> series attributable per workload; do **not** add path/URL context (PII +
> cardinality). This entry emits **nothing** until Task A1+ attach an L7 policy to a
> surface, that is expected, it is a prerequisite, not a trigger.

**Step 2:** Bump the cilium chart if its Application pins a version
(`grep -nE 'targetRevision|chart:|repoURL' projects/platform/cilium/application.yaml`).

**Step 3:** `format`; commit `feat(cilium): enable httpV2 Hubble metric (inert until L7 policy)`.

**Rollback:** revert both list edits.

---

### Task A1: Harden `/invoke` with an L7 rule + its alerts

**Files:**
- Modify: `projects/firecracker/substrate/chart/templates/cilium-policy.yaml` (the `<release>-invoke` policy)
- Create: `projects/platform/signoz-addons/alerts/templates/hubble-invoke-http.yaml`

**Context:** The policy already restricts `/invoke` ingress to the monolith
namespace at L3/L4 and is gated by `ciliumPolicy.enabled` (default false); the
daemon's TokenReview is the primary gate, this is defense-in-depth.

**Step 1 (enforce):** Add an L7 HTTP rule to the existing ingress `toPorts` so only
the real invoke method/path is allowed:

```yaml
      toPorts:
        - ports:
            - port: "<invoke-port>"   # confirm from the rendered chart
              protocol: TCP
          rules:
            http:
              - method: "POST"
                path: "/invoke"
```

**Step 2 (shadowing check, mandatory):** confirm no other rule in this policy (or a
sibling selecting the same endpoint) adds an L4 allow covering `<invoke-port>`
without an `http` block. If one exists the L7 rule is a no-op; fold them together.

**Step 3 (enable):** set `ciliumPolicy.enabled: true` for substrate in
`deploy/values.yaml` if not already (enabling the policy is a separate call from
writing the rule). `/invoke` is a low-QPS control path, so the Envoy cost is
negligible.

**Step 4 (observe):** add a SigNoz alert over `hubble_httpv2_requests_total`
filtered to the substrate/`/invoke` destination workload, firing on a sustained 5xx
fraction (and optionally a p99-latency alert over the httpV2 duration histogram).
Model on `pod-restart-rate.yaml`; channel `incidentio`. This series exists only
because Step 1's policy now redirects `/invoke` through Envoy.

**Step 5:** render-check
(`helm template substrate projects/firecracker/substrate/chart -f projects/firecracker/substrate/deploy/values.yaml | grep -A20 'rules:'`),
bump the substrate chart, `format`, commit
`feat(substrate): L7 POST /invoke enforcement + HTTP alert`.

**Rollback:** remove the `rules.http` block, revert `ciliumPolicy.enabled`, delete
the alert (its series disappears with the policy anyway).

---

### Task A2: Restrict a single-purpose internal API surface + its alerts

**Files:**
- Modify: the relevant `CiliumNetworkPolicy` (likely
  `projects/monolith-public/chart/templates/cilium-policy.yaml` or the monolith
  chart, depending on where the internal call lands)
- Create: a matching SigNoz alert template

**Step 1 (identify, load-bearing):** the ADR example is "the frontend may only
`POST /internal/chat`, not arbitrary backend paths." Verify a genuinely
**single-purpose** internal endpoint exists before restricting:

Run: `grep -rniE "/internal/|/api/agent/|internal.*chat" projects/monolith/*/routers* projects/monolith-public 2>/dev/null | head`
Pick a caller→callee pair where the caller legitimately hits exactly one
method/path. **Do not** attach an L7 path rule to a multi-path service (e.g. the
public `web` pod serving many routes): the `http` block is an allow-list, so every
other route would be denied.

**Step 2 (enforce):** add the L7 rule to the callee's ingress `toPorts`, scoped to
the verified method/path. Apply the same shadowing check as A1 Step 2.

**Step 3 (observe):** add the golden-signal alert for this destination workload
over `hubble_httpv2_requests_total` / duration histogram.

**Step 4:** render-check; use `hubble observe --verdict DROPPED` to confirm the
intended call is allowed and a different-path probe is denied; bump the affected
chart; `format`; commit `feat(<svc>): L7 method/path enforcement + alert on <surface>`.

**Rollback:** remove the `rules.http` block and the alert.

> **Cadence:** A1 and A2 are the first two surfaces. Additional surfaces are added
> the same way, one PR each (policy + its alert), as they are identified. This is
> ongoing per-surface work, not a finite task list.

---

## PART B: default-deny (#4, GATED, do not start before B1 is signed off)

### Task B1: Coverage audit (the gate)

This resolves ADR Open Question #4. It is investigation, not a code change, and its
output is the go/no-go for Task B2.

**Step 1:** With policy still in `default` mode, enable Hubble flow visibility and
enumerate every flow in the cluster over a representative window (a full day
covering scheduled jobs, CronWorkflows, and the AI/inference paths on node-4):

Run: `kubectl exec -n kube-system ds/cilium -- hubble observe --output jsonpb --since 24h > /tmp/flows.json`
(or stream over the window); aggregate by (source workload, destination workload, port).

**Step 2:** Build the coverage matrix: for every observed source→destination flow,
record whether a `CiliumNetworkPolicy` already selects the destination and allows
that source/port. Flag every uncovered legitimate flow, exactly the flows that
would break under default-deny.

**Step 3 (deliverable):** a checked-in audit note listing every workload and
whether it has an ingress policy, every uncovered flow that must get a policy before
the flip, and confirmation that scheduled/rare paths (backups, migrations, model
cache jobs) were observed in the window.

**Gate:** Task B2 does not proceed until this matrix shows zero uncovered legitimate
flows (either covered, or a policy PR staged to cover them).

---

### Task B2: Add policies for any gaps, then flip to default-deny

**Do not start until B1 shows full coverage.**

**Files:**
- Modify: `CiliumNetworkPolicy` templates for any workloads the audit found
  uncovered (staged as ordinary PRs first, verified live in `default` mode where a
  missing allow is harmless)
- Modify (the flip): `projects/platform/cilium/values.yaml` and
  `projects/platform/cilium/bootstrap/cilium-helmchart.yaml`:
  `policyEnforcementMode: always`

**Step 1:** Land the gap-filling policy PRs first and confirm in `default` mode (via
`hubble observe`) that each new policy allows exactly the intended flows. Because
mode is still `default`, a mistake here cannot cause a deny outage yet.

**Step 2:** Flip `policyEnforcementMode: always` in both files (keep in sync). Treat
this as a **windowed change with immediate rollback**: the moment it syncs, every
workload with no selecting policy is denied.

**Step 3 (watch the drop alert):** the Hubble policy-drop alert from the
observability plan is the primary signal here. A spike after the flip is a missed
flow. Rollback is reverting the one value.

**Step 4:** Update `docs/security.md` to record default-deny as the steady-state
posture.

**Rollback:** revert `policyEnforcementMode` to `default` in both files; ArgoCD
re-opens the unselected workloads within a sync.

---

## Verification

- Part A: per surface, confirm the `httpV2` series appears in SigNoz *after* the
  policy syncs (it will not exist before); `hubble observe --verdict DROPPED` shows
  the intended method/path allowed and off-list paths denied; re-read the rendered
  policy to confirm no L4 allow shadows the L7 rule.
- Part B: after the flip, the drop alert stays quiet and app health
  (`jomcgi.dev/health`, private tier) is green. Any drop is a missed flow; roll back
  the single value.

## References

| Resource | Relevance |
| -------- | --------- |
| ADR networking/003 | Capabilities #2 + #4 this plan implements |
| `projects/monolith-public/chart/templates/cilium-policy.yaml:115-122` | The L4-shadows-L7 gotcha, documented for DNS |
| `projects/firecracker/substrate/chart/templates/cilium-policy.yaml` | The `/invoke` policy A1 hardens |
| `docs/plans/2026-07-13-hubble-signoz-observability.md` | Builds the shared `hubble-metrics` scrape job these alerts read from |
| [Cilium L7 protocol visibility](https://docs.cilium.io/en/v1.19/observability/visibility/) | Why L7 metrics require an L7 policy |
| [CiliumNetworkPolicy L7 HTTP](https://docs.cilium.io/en/stable/security/policy/language/#layer-7-examples) | Method/path rule syntax |
| [policyEnforcementMode](https://docs.cilium.io/en/stable/security/policy/intro/#policy-enforcement-modes) | `always` = default-deny semantics |
