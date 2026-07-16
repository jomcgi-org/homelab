# EmberVM R3 Handoff: finish serving + fix node-Envoy→VM networking

## Goal for the new session
Complete R3 (warm request serving): get an end-to-end HTTP 200 from a real serving VM through the node Envoy, then close R3 (PR-8: record gate numbers, mark R3 shipped in ADR embervm/001). The ONLY thing left is the node-Envoy→serving-VM data-plane hop, blocked by a network-namespace issue whose fix Joe has already decided (route through noded via DNAT).

## Current state (all merged, chart 0.1.61 deployed and Synced/Healthy)
R3 serving is code-complete and PROVEN LIVE through cold-boot + publish; only the final Envoy→VM hop fails.
- 6 boot/data-plane fixes merged (#3567–#3572, chart 0.1.60): base-provisioning handler-disk, FC sector-floor pad, init=, /proc mount, hostNetwork Envoy, dynamic base-id.
- Stale-base fix (#3574, 0.1.61): noded reports only serving images whose runtime is still provisioned (kills the transient post-roll "runtime not provisioned" 503).
- Docs (#3573 merged): DECISIONS.md D-R3.11.1/2/3 (repo root) record every fix above.

### Proven live (gate evidence gathered this session)
- Serving cold-boot works end to end up to publish: base build → padded handler.zip on /dev/vdb → `init=/usr/local/bin/ember-runtime-guest-init` → /proc mounted → EMBER_SERVING_PORT/EMBER_HANDLER_ZIP set → shim logs `serving on TCP 0.0.0.0:8080 ... (ready)` → noded health gate passes → control plane publishes (`status.serving = live:1, published:1`).
- Task-lane regression PASSES live: `og-image` FaaS returns HTTP 200 image/png (22462 bytes) via `POST/GET http://monolith.monolith.svc:8000/functions/og-image?title=...`. The byte-unchanged-for-task/session invariant holds in prod.
- Node Envoy is healthy (0.1.61), the serving endpoint is published to it (Envoy `/stats`: `cluster.serve|serving-og-image.membership_healthy: 1`).

## THE blocker (root cause, confirmed)
The node Envoy cannot connect to the serving VM: Envoy `/stats` shows `upstream_cx_connect_fail`, and a request returns 503 "upstream connect error ... reset reason: connection timeout".
- Serving VMs live in **noded's POD network namespace**: bridge `embervm-serv0` (172.31.0.1/24), VM taps + IPs (e.g. 172.31.0.2). Confirmed: noded is NOT hostNetwork (podIP 10.42.x); the hostNetwork serving-envoy pod does NOT see `embervm-serv0` at all.
- The node Envoy is a SEPARATE DaemonSet pod, so it's in a different netns (pod-net or host-net), and **K8s cannot share a netns across pods**. So it can never route to noded's pod-netns bridge. noded reaches the VM (same netns → health passes → live:1); the Envoy times out.
- My hostNetwork (#3571) + dynamic-base-id (#3572) changes were treating the wrong problem: they got the Envoy healthy but in the wrong netns. **They must be REVERTED.**

## THE decision (Joe-confirmed): route through noded via DNAT, xDS still publishes the endpoint
Keep the VM tap IP internal to noded's netns. Have noded expose each live serving VM on **noded's own routable pod IP** via a per-VM TCP port + a kernel **DNAT** (nftables) to the VM's tap IP:8080. The control plane publishes `noded_pod_IP:port` as the serving endpoint via xDS (same push mechanism, different endpoint value — "xds still pushes the route details back to envoy for getting traffic in"). The node Envoy (reverted to the pod network) dials `noded_pod_IP:port`; the kernel DNATs to the VM. noded stays OFF the hit path (kernel conntrack forwarding, not a userspace proxy). Record this as D-R3.11.4 in DECISIONS.md (netns root cause + route-through-noded + the hostNetwork revert).

## Implementation plan
### noded (Go) — projects/embervm/noded/
1. **Per-VM port allocation**: allocate a host-reachable TCP port per live serving VM on noded's pod IP (extend `serving/alloc.go` ipAllocator or add a port allocator). Free on VM stop.
2. **DNAT rule per VM**: `serving/net.go` currently installs ONLY a forward-filter chain (`nftRuleset`, comment says "v1 does not [NAT]"). Add a nat chain (`type nat hook prerouting`) in the `embervm_serving` table with a per-VM rule: `ip daddr <noded_pod_IP> tcp dport <vm_port> dnat to <vm_tap_IP>:8080`. Add/remove per VM on start/stop. The existing forward chain (`policy accept`, `established,related accept`) allows the DNAT'd inbound + replies; the VM-egress drop (`iifname bridge ct state new drop`) does NOT block DNAT'd inbound (dest=VM enters via eth0, not the bridge). Ensure `net.ipv4.ip_forward=1` in noded's netns. Get noded's pod IP from the downward API (env `status.podIP`).
   - Watch the whole-table flush model: `nftRuleset` flushes+recreates the table wholesale. Either regenerate the full ruleset including all live-VM DNAT rules on each change, or switch to per-rule add/delete (nft handles or comments). Keep it idempotent + table-scoped.
3. **Endpoint reporting**: `finishServingStart` (serving.go) → `serving_registry.go` servingEntry → NodeStatus.serving_vms. Report **noded_pod_IP + the allocated vm_port** as the endpoint (not the tap IP). KEEP the tap IP internally for the **health probe** (probe.go still probes the VM's tap IP:8080 directly — noded is in-netns, that works). Only the PUBLISHED endpoint changes.
### control plane (Elixir) — projects/embervm/control/lib/embervm/
4. `endpoint_publisher.ex` reads NodeStatus.serving_vms ip:port and pushes to Envoy via xDS. It should publish whatever ip:port noded reports → likely NO change needed. VERIFY it uses the reported ip:port verbatim (it renders the `serve|<workload>` EDS cluster endpoints).
### chart — projects/embervm/chart/templates/
5. **Revert the wrong Envoy changes**: `serving-envoy-daemonset.yaml` → remove `hostNetwork: true` + `dnsPolicy: ClusterFirstWithHostNet` (#3571) and `--use-dynamic-base-id` (#3572). The Envoy goes back to the POD network (it now dials noded's pod-network-routable IP). Keep the tcpSocket:10000 probe, RDS initial_fetch_timeout, stats listener. The serving Service + xDS wiring are unchanged.
6. Bump the embervm chart (`bazel/tools/git/bump-chart.sh projects/embervm`) in the same PR.
### tests
7. Go: nat-ruleset generation (pure fn table test in net_test.go); port alloc/free; endpoint reports noded_IP:vm_port; DNAT add/remove on start/stop. (DNAT itself is live-only — no netns/KVM in CI.)

## Live verification (the gate — CI green ≠ working, no KVM/netns in RBE)
1. Merge → ArgoCD sync (chart publishes to ghcr OCI; if ArgoCD shows OutOfSync/ComparisonError "not found", force `monolith-k8s-sync-argocd-app embervm` after the chart is pullable via `helm pull oci://ghcr.io/jomcgi/homelab/charts/embervm --version <v>`).
2. Drill (in-cluster; CF Access blocks workstation curl): `kubectl run drill --image=curlimages/curl:8.10.1 --rm --attach -n embervm --command -- curl -s -m50 -w '%{http_code} %{content_type}' -H 'Host: og-image-serving.private.jomcgi.dev' 'http://10.43.236.150:10000/?title=test'` (embervm-embervm-serving svc :10000). First request may 504 (cold-boot > Envoy 15s route timeout); the VM comes up (live:1, published:1) and a follow-up returns **200 image/png**.
3. Confirm Envoy `/stats` (via svc :9902/stats): `cluster.serve|serving-og-image.upstream_rq_2xx` increments and `upstream_cx_connect_fail` stops; the published endpoint is `noded_IP:vm_port`.
4. Regression: task-lane og-image FaaS still 200 (`http://monolith.monolith.svc:8000/functions/og-image?title=x`); task/session vsock boots unaffected.
5. Then PR-8: record gate numbers, mark R3 shipped in ADR embervm/001.

## Gotchas / context
- **Part B (public og-image traffic flip) stays GATED on Joe's explicit go — do NOT flip.** Everything here is the PRIVATE drill only. The task-class `og-image` FaaS is untouched and is the instant rollback.
- ArgoCD can wedge: a sync health-waiting on a crash-looping pod hangs forever (selfHeal can't start while an op runs). Terminate via ArgoCD UI, or `kubectl -n argocd patch application embervm --type=json -p '[{"op":"remove","path":"/operation"}]'` then re-sync (this is how the session-blocking wedge was cleared; kubectl patch is normally auto-mode-denied — Joe authorized it).
- Serving scales to zero (idleBank600); the FIRST request through the node Envoy triggers the activator → cold-boot. The activator path runs THROUGH the node Envoy, so the Envoy must be healthy to trigger cold-boots at all.
- og-image serving workload: `serving-og-image` (serving class), host `og-image-serving.private.jomcgi.dev`, port 8080, healthPath `/shim/healthz`, current base `__0865f0c93198`.
- Key files: noded `serving/{net.go,alloc.go,probe.go}`, `server/{serving.go,serving_registry.go,server.go}`, `fcvm/driver/driver.go`; control `endpoint_publisher.ex, serving_manager.ex, serving_placement.ex, node_registry.ex`; chart `templates/serving-envoy-daemonset.yaml, serving-service.yaml, serving-envoy-configmap.yaml`.
- DECISIONS.md (repo ROOT) has D-R3.11.1/2/3. Add D-R3.11.4.
- Open worktrees to clean up: `/tmp/claude-worktrees/r3-followups` (branch docs/embervm-r3-drill-followups — this handoff doc lives here; commit it or copy it), plus the merged fix branches.
