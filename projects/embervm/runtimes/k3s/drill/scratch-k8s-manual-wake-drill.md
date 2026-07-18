# scratch-k8s manual wake drill (TEMPORARY hands-on process)

A **manual, direct-`kubectl`** drill for validating that the `scratch-k8s` composite
group boots and forms a cluster. It drives the wake by port-forwarding the serving
entry and talking to the k3s API directly, and reads its proof from `noded` logs.

**This is a temporary testing process.** It exists because there is not yet
first-class support for driving + observing a group boot (proper workload / group
boot scripts, a boot-status surface, and safe rolls are the intended replacements).
Until those land, this is how a human confirms a composite boot end to end. The
sibling `scratch-k8s-drill.md` is the *acceptance* drill via the wired monolith
`run_python` consumer (`SCRATCH_K8S_KUBECONFIG`); use this manual one when that path
is not set up, or when you need to watch the boot from the outside while debugging.

## Status (what this drill has and has not proven)

As of the composite-boot fix chain (chart `0.1.100`: PRs #3638 guest-init runs k3s,
#3641 tmpfs data dir, #3642 full read-only-root writable set):

- **Proven live**: all three members fresh-boot k3s, stage + import the airgap, and
  the k3s **server reaches Ready** (`Successfully registered node node="10.101.0.10"`
  + `just became ready`, with `kube-apiserver` / `kubelet` / `kube-proxy` running).
- **Not yet proven**: the agents completing their join (a full 3-node Ready cluster),
  and `kubectl get nodes` answering **through the entry** (port 5410), which currently
  returns `EOF` at the TLS layer. That EOF is a serving-entry -> k3s `:6443` DNAT
  question, NOT a guest-boot problem (Step 2's log proof is independent of it).

## Step 0: wait for a STABLE, no-deploy window

A `noded` roll destroys the composite group: the per-group bridges and the member
microVMs die with the pod, and warmth is pinned to that node's local disk. So any
embervm deploy rolling mid-drill kills the cluster you are testing. Before starting:

```bash
kubectl get application -n argocd embervm -o jsonpath='{.status.sync.status}{"\n"}'   # want: Synced
kubectl get pods -n embervm -l app.kubernetes.io/name=embervm-noded                   # want: 1/1 Running, AGE > ~5m, not Pending
NODED=$(kubectl get pod -n embervm -l app.kubernetes.io/name=embervm-noded -o name | head -1)
```

(Non-destructive rolls, off-node snapshot storage, and multi-node scheduling are the
HA-noded work that will make this precondition unnecessary.)

## Step 1: build the kubeconfig

The kubectl token is the stable per-group secret (`spec.group.secretRef`), so it
survives banks/relights/rolls.

```bash
TOKEN=$(kubectl get secret -n embervm embervm-embervm-scratch-k8s -o jsonpath='{.data.EMBER_GROUP_SECRET}' | base64 -d)
cat > /tmp/scratch-k8s.kubeconfig <<EOF
apiVersion: v1
kind: Config
clusters:  [{name: c, cluster: {server: 'https://127.0.0.1:5410', insecure-skip-tls-verify: true}}]
users:     [{name: u, user: {token: "$TOKEN"}}]
contexts:  [{name: x, context: {cluster: c, user: u}}]
current-context: x
EOF
```

## Step 2: watch the boot (the reliable proof)

This is the proof that does NOT depend on the entry routing: each member's guest
console streams to `noded` stdout, so the whole cluster forming is visible in the
`noded` log.

**Terminal A** (tail noded):

```bash
kubectl logs -n embervm $NODED -f --since=5s \
  | grep -iE 'composite member boot|writable tmpfs|Imported images|Successfully registered node|became ready|Starting k3s agent|FATA|panic'
```

**Terminal B** (trigger exactly ONE wake, leave the port-forward up):

```bash
kubectl port-forward -n embervm svc/embervm-embervm-serving 5410:5410 &   # leave running
curl -sk --max-time 5 https://127.0.0.1:5410/ >/dev/null 2>&1 || true      # fires wake-on-connect; the failure is expected
```

**Pass condition (terminal A): three `Successfully registered node` lines** ->
`node="10.101.0.10"` (server), `"10.101.0.11"` (agent-0), `"10.101.0.12"` (agent-1).
That is the 3-node cluster forming, from logs, no API needed.

## Step 3: query through the entry (gate evidence + the open EOF bug)

Once terminal A shows the server Ready and both agents registered, the group
publishes its entry endpoint. Query with a **fresh** connection:

```bash
KUBECONFIG=/tmp/scratch-k8s.kubeconfig kubectl get nodes -o wide
```

Expect 3 nodes, all `Ready`. **If this `EOF`s**, that is the known-open issue: the
boot worked (Step 2) but the serving-entry -> k3s `:6443` DNAT/splice path is the
thing to debug (start at the serving Envoy xDS snapshot for port 5410 and noded's
`PortForIP` rules). It does not invalidate Step 2.

## Gotchas (these WILL bite you)

- **One connection; do not hammer 5410.** Repeated connects trip the per-workload
  parked-connection cap (`park_full` in the control-plane logs).
- **Do not HOLD a connection across publish.** Trigger, watch logs, then query with a
  fresh connection. A connection held across the group's publish is reset when the
  serving Envoy reloads its xDS config, which reads as a dropped/refused connection
  and looks like a failure when it is not.
- **Give it up to ~3 min.** `wakeTimeoutSeconds` is 180; the server is ~30-90s and the
  agents join after it.
- **Nothing boots?** The group is likely mid-relight or was just destroyed by a roll,
  recheck Step 0. `kubectl logs -n embervm <control-plane-pod> -c control-plane`
  shows the wake decision (`embervm group woken` on success, `park_full` denials when
  the entry is being hammered).

## Debugging the entry EOF (Step 3), where to look

- **Is the boot actually done?** Confirm all three `Successfully registered node`
  lines in Step 2 first; the agents health-gate on kubelet `:10250` (startOrder 1),
  and the whole-group wake only publishes after every member gates within
  `wakeTimeoutSeconds`. If an agent is slow to bind `:10250`, the wake can time out
  before publish.
- **Is the group actually published?** The control-plane log should carry
  `embervm group woken` for the workload; the published entry is the DNAT projection
  `{pod_ip, portBase + hostOffset(entry_ip)}` (e.g. `.10` -> `30010`), routed by
  noded's `PortForIP` to the server tap `:6443`. The noded TCP health-gate proving
  `:6443` reachable does NOT prove this client DNAT path is wired.
