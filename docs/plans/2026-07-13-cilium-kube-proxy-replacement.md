# Cilium kube-proxy Replacement Implementation Plan

> **For Claude:** This plan has **two tracks**, like the Cilium migration plan.
> **Track 1 (Git/PR)** is executable via `superpowers:subagent-driven-development`.
> **Track 2 (node operations)** is a **human-executed runbook**: it edits
> `/etc/rancher/k3s/config.yaml` and restarts k3s on physical nodes, so Joe runs
> it by hand. Do **not** dispatch subagents to execute Track 2. It implements
> capability **#3** of ADR
> [networking/003](../decisions/networking/003-cilium-capability-adoption.md).
> **Pair Track 2 with the next already-scheduled node-reboot window** rather than
> opening a dedicated one.

**Goal:** Turn on Cilium's eBPF service load-balancer
(`kubeProxyReplacement: true`) and remove k3s's embedded kube-proxy, eliminating
the iptables service-routing path.

**Architecture:** Per ADR networking/003. Today a Service ClusterVIP is programmed
by kube-proxy iptables on each node; with `kubeProxyReplacement: true` Cilium's
eBPF datapath resolves it. The catch (the whole reason this is a node cycle):
`cilium-agent` currently reaches the API server *through* the kube-proxy-programmed
in-cluster `kubernetes` Service, which is why `k8sServiceHost` is intentionally
unset (`projects/platform/cilium/values.yaml:9-13`). Removing kube-proxy removes
that path, so Cilium must be told the API-server address directly, and that answer
differs for the server nodes (node-1/2/3, local apiserver) versus the node-4 agent
(no local apiserver). Getting `k8sServiceHost` right for node-4 is the single
highest-risk step.

**Tech Stack:** k3s v1.35 HA (node-1/2/3 servers, node-4 agent), Cilium 1.19.5,
`cilium-cli`.

---

## Task 0: Discover the API-server address node-4 uses (do this FIRST)

This resolves ADR Open Question #3 and decides `k8sServiceHost`. It is read-only
and can be done any time before the window.

**Step 1:** On node-4 (the agent), find how it reaches the control plane:

Run: `sudo grep -E 'server:' /etc/rancher/k3s/config.yaml /etc/systemd/system/k3s-agent.service.env 2>/dev/null; sudo cat /var/lib/rancher/k3s/agent/kubelet.kubeconfig 2>/dev/null | grep server`
Expected: a `https://<addr>:6443` the agent was joined with. That `<addr>` is the
candidate `k8sServiceHost`.

**Step 2:** Determine whether `<addr>` is a VIP/LB reachable from ALL nodes, or a
single server IP:
- If it is a fixed VIP (kube-vip / external LB) reachable from every node
  including the servers, use it directly as `k8sServiceHost`.
- If it is a single server node IP, that becomes a single point of failure for
  service routing once kube-proxy is gone. Decide before the window: accept it
  (homelab, acceptable), or stand up a small VIP first. Record the choice in the
  PR description.

**Output:** the chosen `k8sServiceHost` value + `k8sServicePort` (6443).

---

## TRACK 1: Git / PR change (staged ahead of the window)

### Task 1: Set kubeProxyReplacement and k8sServiceHost in Cilium values

**Files:**
- Modify: `projects/platform/cilium/values.yaml`
- Modify: `projects/platform/cilium/bootstrap/cilium-helmchart.yaml` (keep in sync)

**Step 1:** In both files:

```yaml
kubeProxyReplacement: true          # was false
k8sServiceHost: "<from Task 0>"     # was intentionally unset
k8sServicePort: 6443
```

Remove (or update) the existing comment block that explains why `k8sServiceHost`
was unset, since the reasoning inverts once kube-proxy is gone.

> **NOTE:** Merging this alone does NOT remove kube-proxy: k3s still runs it until
> Track 2 sets `disable-kube-proxy`. With `kubeProxyReplacement: true` and
> kube-proxy still present, Cilium runs in a compatible mode; the real switch is
> the node-level k3s flag in Track 2. Staging the value first means ArgoCD has
> already reconciled it when the nodes restart.

**Step 2:** Bump the cilium chart if its Application pins a version (see the
observability plan Task 1 Step 2 for the check).

**Step 3:** `bazel/tools/format/fast-format.sh`; commit
`feat(cilium): enable kubeProxyReplacement with explicit k8sServiceHost`.

**Rollback:** revert; restore the `k8sServiceHost`-unset comment.

---

## TRACK 2: The maintenance window (human-executed runbook)

Pre-req: Track 1 merged and synced; Task 0 value chosen; `cilium` CLI on the
workstation; paired with a node-reboot window.

### Task 2: Safety snapshot

**Step 1:** On node-1: `sudo k3s etcd-snapshot save --name pre-kube-proxy-free`
Expected: snapshot path printed.

**Step 2:** Record baseline:
Run: `cilium status | grep -i kubeproxy; kubectl -n kube-system get ds kube-proxy 2>/dev/null; sudo iptables-save | wc -l`
Expected: `KubeProxyReplacement: False` (or partial), a kube-proxy DaemonSet (if
k3s exposes one) or k3s-managed rules, and a baseline iptables line count.

---

### Task 3: Disable k3s kube-proxy and restart, servers then agent

**Step 1:** On **node-1/2/3 and node-4** append to `/etc/rancher/k3s/config.yaml`:

```yaml
disable-kube-proxy: true
```

**Step 2:** Restart k3s, **servers first, then the agent**, one at a time,
watching each back to `Ready` before the next:

Run (each server): `sudo systemctl restart k3s`
Run (node-4): `sudo systemctl restart k3s-agent`
Expected: node briefly `NotReady`, then `Ready`; `cilium-agent` re-programs
services.

**Step 3 (the critical check, node-4 first):** verify the agent node can still
reach the API server and resolve Services now that its kube-proxy path is gone:

Run: `kubectl -n <ns-with-a-node4-pod> exec <pod> -- wget -qO- https://kubernetes.default.svc:443/healthz --no-check-certificate`
Expected: `ok`. If this fails on node-4, `k8sServiceHost` is wrong for the agent,
**roll back immediately** (Task 3 rollback) and revisit Task 0.

**Step 4:** verify kube-proxy replacement is active cluster-wide:

Run: `cilium status | grep -i kubeproxy`
Expected: `KubeProxyReplacement: True`.

Run: `cilium connectivity test`
Expected: all tests pass.

Run: `sudo iptables-save | wc -l`
Expected: materially fewer rules than the Task 2 baseline (the service NAT chains
are gone).

**Rollback:** remove `disable-kube-proxy: true` from each `config.yaml`,
`systemctl restart k3s` (servers then agent) to bring k3s kube-proxy back; if
service routing is wedged, restore the Task 2 snapshot
(`sudo k3s server --cluster-reset --cluster-reset-restore-path=<snapshot>`).
Revert the Track 1 values PR if backing out fully.

---

### Task 4: Verify end to end and bank

**Step 1:** App-level health:
Run: `curl -s https://jomcgi.dev/health | jq .` and spot-check the private tier.
Expected: healthy.

**Step 2:** Watch the Hubble policy-drop alert (from the observability plan) and
SigNoz for any new connection failures over the next hour. A spike in drops or
5xx after the cutover points at a service the eBPF LB resolves differently.

**Post-cutover follow-up (not this plan):** with eBPF service LB stable, DSR and
Maglev (`loadBalancer.mode=dsr`, `loadBalancer.algorithm=maglev`) become
available as an independent tuning change.

## References

| Resource | Relevance |
| -------- | --------- |
| ADR networking/003 | Capability #3 this plan implements |
| `docs/plans/2026-07-10-cilium-migration.md` | The two-track runbook shape and the k3s node-config precedent |
| `projects/platform/cilium/values.yaml:9-13` | The comment explaining why `k8sServiceHost` is unset today |
| [Cilium kube-proxy-free](https://docs.cilium.io/en/stable/network/kubernetes/kubeproxy-free/) | `kubeProxyReplacement` + `k8sServiceHost` requirements |
| [k3s `disable-kube-proxy`](https://docs.k3s.io/cli/server) | The node-level flag that removes k3s kube-proxy |
