# ADR 003: Incremental Cilium Capability Adoption

**Author:** Joe McGinley
**Status:** Accepted
**Created:** 2026-07-13
**Supersedes:** N/A (builds on [platform/012](../platform/012-cilium-replaces-linkerd.md))

---

## Problem

ADR [platform/012](../platform/012-cilium-replaces-linkerd.md) replaced Linkerd + flannel
with Cilium, but deliberately scoped the migration to "get the datapath swapped
safely" and pushed the higher-value or higher-blast-radius Cilium features to a
follow-up. The migration's post-migration follow-ups list
them explicitly: kube-proxy replacement, `policyEnforcementMode: always`, and a
richer Hubble metric set were all left as "adopt later, independently." Cilium is
now the CNI, but we are running it in close to its most conservative shape:

- **We run egress policy that can silently black-hole traffic, with no alert.**
  The `monolith-public-web-egress` policy is a default-deny-off-cluster gate with a
  single `toFQDNs` allow for `challenges.cloudflare.com`
  (`projects/monolith-public/chart/templates/cilium-policy.yaml`). A wrong or
  drifted policy would drop legitimate traffic, and today the only way we would
  learn is a user report. Cilium already counts these drops
  (`hubble_drop_total`, emitted by the current `["dns","drop","tcp","flow"]` metric
  set); we just do not alert on them.
- **We have neither L7 enforcement nor L7 observability, and in Cilium they are the
  same switch.** Network policy is L3/L4 plus L7-DNS only; there are zero L7 *HTTP*
  policies. The Linkerd `AuthorizationPolicy` objects that constrained *who could
  call what* were translated to label-selected L3/L4 `CiliumNetworkPolicy` ingress
  rules, so method/path-level authorization is not enforced anywhere. The same gap
  removes our L7 *visibility*: Hubble emits `hubble_http_*` metrics only for
  traffic redirected to its per-node Envoy proxy, and in Cilium 1.19 the only way
  to trigger that redirect is a `CiliumNetworkPolicy` with L7 rules. So adding
  `http` to `hubble.metrics.enabled` alone produces no HTTP series; L7 golden
  signals arrive per-surface, as a byproduct of applying L7 policy to that surface.
- **The old service-map DAG is orphaned, not broken.** Easy to conflate with the
  above, so worth stating plainly: the private dashboard's service-map DAG was
  *intentionally retired* (commit `a58c5b1a1`, "replace homepage SLO topology with
  project stack"), and no frontend route consumes the topology endpoint today. But
  the backend that fed it was left running: `/api/home/observability/topology` plus
  the scheduled `topology_rollup()` job (`home/observability/rollup.py`) still
  build a payload from ClickHouse each tick and snapshot it to Postgres for a
  consumer that no longer exists, and `topology_config.py` / `topology_query.py`
  still carry the `EdgeConfig` DAG. That is dead weight to remove, not a feature to
  restore.
- **`kubeProxyReplacement` is `false`.** k3s's embedded kube-proxy still programs
  service load-balancing via iptables. Cilium's eBPF service LB would remove that
  iptables path, but the migration left it off to keep blast radius down.
- **`policyEnforcementMode` is `default`** (allow-until-selected). Any workload
  with no policy selecting it is fully open. This was correct at cutover (tearing
  out Linkerd's authz caused no deny-gap), but it is the weakest steady-state
  posture.

Why now: the drop alert is the cheapest possible safety net for the egress gates
we just started running, and the L7 policy/observability arc restores the
authorization granularity we lost with Linkerd while giving back per-service golden
signals as a byproduct. The rest are the genuinely-deferred follow-ups from ADR
012, and this ADR ranks them so we adopt in value-per-effort order rather than
reaching for the biggest, riskiest one first.

---

## Decision

Adopt four Cilium capabilities **incrementally, ranked by value-per-effort**, not
as one change. The ranking *is* the decision. A correction from the first draft is
baked into it: L7 HTTP *metrics* and L7 HTTP *policy* are not two capabilities but
one, because Cilium produces L7 observability only for traffic an L7 policy has
redirected to Envoy. So "get the golden-signal metrics back" is not a cheap
standalone step; it rides on the per-surface L7 policy work and shares its cost.
The genuinely cheap, standalone win is the drop alert, which needs only the
already-emitted `drop` metric. It goes first.

| # | Capability | Value | Effort | When |
| - | ---------- | ----- | ------ | ---- |
| 1 | **Hubble drop-alert.** Scrape `hubble_drop_total`, alert on a sustained policy-denied drop rate. | High operational: turns a silent egress-policy mistake into a seconds-latency alert instead of a user report. | Low: one scrape target + one alert rule; `drop` is already emitted, no datapath change. | **Now** |
| 2 | **L7 HTTP `CiliumNetworkPolicy`, per sensitive surface, enforcement + the golden-signal metrics it unlocks.** Enforce method/path (e.g. frontend may only `POST /internal/chat`; `/invoke` restricted at L7); the same policy makes Hubble emit `httpV2` metrics for that surface, from which we build per-service error-rate / p99-latency alerts. | High: restores the method/path authorization Linkerd's `AuthorizationPolicy` gave, *and* yields L7 golden-signal alerting we have never had, in the pane we already watch. | Medium, incremental: one surface at a time; each surface is one policy + its alerts, sharing the same Envoy-redirect cost. | **Per surface, ongoing** |
| 3 | **`kubeProxyReplacement: true`.** Cilium eBPF service LB replaces k3s kube-proxy's iptables path. | Real but not urgent on a 4-node homelab: lower service-path latency, better scaling, unlocks DSR/Maglev later. | Larger: needs `k8sServiceHost`/`Port` care for the mixed server/agent topology and a node cycle. | **Paired with the next node-reboot window** |
| 4 | **`policyEnforcementMode: always`** (default-deny). | Strongest posture: every unselected workload flips from open to denied. | High + highest blast radius: requires auditing that every legitimate flow has a policy first. | **Deferred, gated on coverage audit** |

### What this ADR does *not* do

- It does **not** rebuild the service-map DAG. That visualization was retired on
  purpose (`a58c5b1a1`); resurrecting it on Hubble metrics would rebuild a consumer
  we deleted. #2 targets alerts, not a map, and the orphaned
  `/api/home/observability/topology` endpoint, its scheduled rollup, and the
  `EdgeConfig` DAG code are removed as cleanup. (If a visual service map is ever
  wanted again, Cilium's own Hubble UI renders one natively, one Helm flag, no
  bespoke code, see Alternatives.)
- It does **not** enable `http` metrics globally. There is no such thing as "global
  L7 metrics" without pushing all traffic through Envoy; metrics come per-surface
  with the L7 policy, which is why #2 is incremental.
- It does **not** adopt Cilium's SPIFFE mutual authentication. ADR 012 already
  deferred that; nothing here revisits it.
- It does **not** flip to default-deny now. #4 is recorded as the intended
  destination and its precondition (complete L3/L4 policy coverage) is named, but
  the flip is out of scope until that audit exists. The #2 work builds toward it.
- It does **not** enable DSR or Maglev in the same step as #3. Those are
  `loadBalancer` tuning that only makes sense *after* `kubeProxyReplacement: true`
  is stable; a later, independent config change.

Implementation is tracked in GitHub Issues, not in this ADR:

- Hubble/SigNoz observability (#1: the drop alert, the
  shared `hubble-metrics` scrape job, and the topology decommission)
- Cilium policy hardening (#2 + #4: L7 per-surface
  policies with their harvested HTTP alerts, then the gated default-deny flip)
- kube-proxy replacement runbook (#3, a two-track runbook
  like the migration plan)

---

## Architecture

Two independent metric paths share one SigNoz scrape target (`hubble-metrics` on
:9965). The drop alert (#1) needs only the already-emitted `drop` metric. The L7
golden-signal alerts (#2) need `httpV2` metrics, which only exist for surfaces that
have an L7 policy.

```mermaid
graph LR
    subgraph "per node"
        CA[cilium-agent + Hubble] -->|:9965 /metrics| HM[hubble-metrics Service]
    end
    L7[L7 CiliumNetworkPolicy\non a surface] -.redirects that surface's\ntraffic to per-node Envoy.-> CA
    HM -->|scrape job| SC[SigNoz otel-collector\nprometheus receiver]
    SC --> CH[(ClickHouse\nsignoz_metrics)]
    CH -->|hubble_drop_total\n(always emitted)| A1[SigNoz alert\npolicy-drop rate]
    CH -->|hubble_httpv2_requests_total\n+ duration histogram\n(only where L7 policy exists)| A2[SigNoz alerts\nper-service error-rate + p99 latency]
```

**L7 is an opt-in with a datapath cost, and observability rides on enforcement.**
Cilium's datapath is pure L3/L4 by default. Applying a `CiliumNetworkPolicy` with
L7 (`rules.http`) matchers to a surface redirects *that surface's* matched flows
through Cilium's per-node Envoy proxy so they can be parsed. That redirect is what
enables both L7 enforcement and, as a byproduct, the `httpV2` metrics Hubble emits.
It is the sidecarless-mesh mechanism, but it is not free: parsed flows take an
extra hop and some latency. This is why #2 is applied per-surface, not globally,
keeping the set of Envoy-parsed flows to the surfaces whose method/path enforcement
(and golden signals) actually earn the cost. It is also why we do **not** just turn
on the Hubble UI to get a service map back: the UI is native and cheap to enable,
but it is exploratory (no alerting into our incidentio channel) and is a second
observability surface to run and secure, which we opted out of when we made SigNoz
the single pane. (`httpV2` rather than the deprecated `http`: it folds HTTP status
onto `hubble_httpv2_requests_total`, which the error-rate alert needs; the v1
`http` metric keeps status on a separate `hubble_http_responses_total`.)

Capability #3 changes the service datapath rather than adding an exporter: today a
Service ClusterVIP is resolved by k3s kube-proxy's iptables rules on each node;
with `kubeProxyReplacement: true`, Cilium's eBPF datapath resolves it directly. The
nuance that makes this windowed: with kube-proxy present, `cilium-agent` reaches
the API server *through* the in-cluster `kubernetes` Service that kube-proxy
programs, which is why `k8sServiceHost` is intentionally unset today
(`projects/platform/cilium/values.yaml:9-13`). Removing kube-proxy removes that
path, so Cilium must be told the API-server address directly, and the answer
differs for servers (node-1/2/3, local apiserver) versus the node-4 agent (no local
apiserver). That is the highest-risk part of the change, and getting it wrong
strands node-4; the node cycle itself (setting `disable-kube-proxy` and restarting
k3s per node) is why it needs a window at all.

Capability #4 adds no new component: it flips a single Helm value once coverage is
proven.

---

## Alternatives Considered

- **Treat L7 HTTP metrics as a cheap standalone capability (the first-draft #1).**
  Rejected as incorrect: Cilium emits no `hubble_http*` series without an L7 policy
  redirecting the traffic to Envoy, so "just add `http` to the metric list" yields
  nothing. L7 observability is folded into #2, where the enabling policy lives.
- **Get L7 visibility via a pod annotation instead of a policy.** Not available:
  the old `proxy-visibility` annotation path was removed; Cilium 1.19 documents L7
  visibility only via a `CiliumNetworkPolicy` with L7 rules.
- **Enable L7 visibility globally (allow-all `http: [{}]` cluster-wide) to get
  metrics everywhere.** Rejected: that pushes *all* cluster traffic through the
  per-node Envoy, a latency cost on every flow, for observability we only want on a
  few surfaces. Per-surface keeps the Envoy-parsed set small.
- **Rebuild the service-map DAG on Hubble edge metrics.** Rejected: the DAG was
  retired on purpose (`a58c5b1a1`). Rebuilding it would resurrect a consumer we
  deleted; the DAG code is decommissioned, not ported.
- **Enable the Hubble UI instead of feeding SigNoz.** Rejected as the primary path,
  noted as the cheap way to get a *visual* service map back if ever wanted. Cilium
  ships a native service dependency map in Hubble UI (one Helm flag). We keep it off
  because SigNoz is the single pane (`values.yaml`: "UI off; SigNoz remains the
  dashboard") and, decisively, Hubble UI does not alert into incidentio, which is
  the actual goal.
- **Do `kubeProxyReplacement` standalone, now.** Rejected: it needs a node cycle
  regardless, so bundling it with the next already-scheduled node-reboot window is
  strictly cheaper in disruption than a dedicated window.
- **Flip `policyEnforcementMode: always` now to get the posture win immediately.**
  Rejected: with today's partial policy coverage this would deny every unselected
  workload and cause a broad outage. Gated behind a coverage audit on purpose; the
  single highest-blast-radius item on the list.
- **Adopt DSR / Maglev alongside #3.** Deferred: only meaningful after eBPF service
  LB is stable, and adds tuning variables to an already-windowed change.

---

## Security

Baseline: `docs/security.md`. Net posture effect of this ADR:

- **#1 (drop-alert) is a defensive win with no new attack surface.** It makes the
  egress default-deny gates *observable*: a misconfigured `toFQDNs` allow or a
  drifted policy that black-holes legitimate traffic becomes an alert in seconds
  instead of a user report. It is the cheapest safety item and lands first.
- **#2 (L7 HTTP policy) restores authorization granularity we lost, and adds
  visibility.** Linkerd's `AuthorizationPolicy` could constrain callers; the L3/L4
  translation kept *who* but dropped *what* (method/path). L7 HTTP rules re-express
  "the frontend may only `POST /internal/chat`, not arbitrary backend paths" and
  harden the already-TokenReview-gated `/invoke` at the network layer
  (defense-in-depth); the same policy yields the golden-signal metrics. **Two
  load-bearing caveats.** (a) Cilium resolves overlapping rules toward the more
  permissive L4, so an L4 allow covering the same port *shadows* an L7 HTTP rule and
  silently nullifies both its enforcement and its visibility. The existing policy
  file documents this for DNS
  (`monolith-public/chart/templates/cilium-policy.yaml:115-122`); every L7 rule must
  ensure no L4 allow covers its port. (b) An L7 `http` block is an allow-list:
  anything not listed is denied, so restricting a *multi-path* service to one
  method/path breaks its other routes. Apply L7 rules only to genuinely
  single-purpose surfaces.
- **Request path is not exposed by this work.** Hubble *metric* labels carry
  method, status, and the workload/namespace context only; request path/URL is not
  an available metric label. Path appears solely in flow logs (`hubble observe` /
  flow export), so the PII concern applies to flow export, not this scrape. The
  metric context is still scoped narrowly (workload + namespace + method + status)
  for cardinality.
- **#4 (default-deny) is the strongest posture but is gated, not deferred
  indefinitely.** It is the destination; its precondition is that every legitimate
  flow is covered by a policy. #2's per-surface work and #1's drop visibility (you
  can *see* what gets denied) are what make the eventual audit tractable. Until that
  audit exists, `default` stands.
- **#3 is posture-neutral.** It changes the service datapath but not the trust
  model.

The "never K8s NetworkPolicies in meshed namespaces" caveat retired with Linkerd
(ADR 012); this ADR does not reintroduce any Linkerd-era constraint.

---

## Risks

| Risk | Likelihood | Impact | Mitigation |
| ---- | ---------- | ------ | ---------- |
| Drop-alert too noisy (benign drops) or too quiet (wrong reason filter) | Medium | Low | Alert on the policy-denied drop *reason* and a sustained rate, not raw `hubble_drop_total`; tune against a baseline window |
| An L7 HTTP policy is shadowed by an existing L4 allow and enforces (and observes) nothing | Medium | Medium | Documented gotcha; per-rule review that no L4 allow covers the L7 port; test with `hubble observe --verdict DROPPED` |
| An L7 allow-list rule on a multi-path service breaks its other routes | Medium | Medium | Apply L7 rules only to single-purpose surfaces; enumerate the surface's real paths before restricting |
| L7 parsing adds latency to matched flows (Envoy hop) | Medium | Low | Scope L7 policy to surfaces that need it; measure with the harvested latency metric; back out if the cost is visible |
| L7 golden-signal alerts never fire because no L7 policy exists on that surface (so no `httpV2` series) | Medium | Low | The alert for a surface ships in the same PR as that surface's L7 policy; verify the series exists via a live scrape before finalizing the alert |
| `kubeProxyReplacement: true` breaks apiserver reachability on node-4 (agent, no local apiserver) | High if `k8sServiceHost` set naively | High | Set `k8sServiceHost` to a value valid cluster-wide (a reachable server VIP/LB), not `127.0.0.1`; validate on node-4 explicitly; do it in a window with `cilium connectivity test` |
| kube-proxy replacement leaves orphan iptables rules or dual-programs services | Medium | Medium | Follow Cilium's kube-proxy-free guidance; `iptables-save` diff before/after; node cycle so nodes come up clean |
| Decommissioning `/api/home/observability/topology` breaks an unknown caller | Low | Low | Grep confirmed no frontend consumer; before deleting, check for external/MCP callers and remove the scheduled `topology_rollup` job registration in the same change |
| Someone flips `policyEnforcementMode: always` before coverage is audited | Low | High | ADR records it as gated; the policy-hardening plan makes the audit an explicit precondition, not a step to skip |

---

## Open Questions

1. **Drop-alert threshold.** Alert on any policy-denied drop, or a rate above a
   baseline? Resolved in the observability plan; leaning "policy-denied reason,
   sustained rate > 0" so a genuine black-hole fires but transient drops do not.
2. **First L7 surfaces.** Which surfaces earn L7 policy first? `/invoke` and one
   single-purpose internal API are the opening candidates; the policy-hardening plan
   verifies each is genuinely single-path before restricting.
3. **`k8sServiceHost` target for kube-proxy-free.** What single address is valid
   from all four nodes including the node-4 agent? Resolved in the kube-proxy plan
   after checking how node-4 currently reaches the apiserver.
4. **Coverage-audit definition for #4.** What counts as "every legitimate flow has a
   policy"? Likely: run in `default` with Hubble flow logging, enumerate all
   observed flows, confirm each is intended and policy-covered, *then* flip. Defined
   in the policy-hardening plan; not a near-term action.

---

## References

| Resource | Relevance |
| -------- | --------- |
| [ADR platform/012](../platform/012-cilium-replaces-linkerd.md) | The migration this builds on; deferred these capabilities on purpose |
| Cilium migration follow-ups (GitHub Issues) | Where #3/#4 were first parked |
| `projects/platform/cilium/values.yaml` | Current `hubble.metrics`, `kubeProxyReplacement`, `k8sServiceHost`, `policyEnforcementMode` |
| `projects/monolith-public/chart/templates/cilium-policy.yaml` | Existing L3/L4 + L7-DNS policy; the L4-shadows-L7 gotcha lives here |
| `projects/firecracker/substrate/chart/templates/cilium-policy.yaml` | `/invoke` policy that #2 hardens at L7 |
| `projects/monolith/home/observability/topology_config.py`, `topology_query.py`, `rollup.py` | The orphaned `/api/home/observability/topology` service map + rollup that #1's plan decommissions |
| `projects/platform/signoz/values-prod.yaml` | SigNoz prometheus scrape configs; the removed `prometheus/linkerd-proxy` job is the template for the Hubble job |
| `projects/platform/signoz-addons/alerts/templates/` | SigNoz alert ConfigMap format for the Hubble alerts |
| [Cilium L7 protocol visibility](https://docs.cilium.io/en/v1.19/observability/visibility/) | L7 metrics require a CiliumNetworkPolicy with L7 rules (no annotation path) |
| [Hubble metrics reference](https://docs.cilium.io/en/stable/observability/metrics/) | `httpV2` / `drop` metric names and labels |
| [Cilium kube-proxy replacement](https://docs.cilium.io/en/stable/network/kubernetes/kubeproxy-free/) | `kubeProxyReplacement` + `k8sServiceHost` requirements |
