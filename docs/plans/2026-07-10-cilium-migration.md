# Cilium Migration Implementation Plan

> **For Claude:** This plan has two tracks. **Track 1 (Git/PR changes)** is
> executable via `superpowers:subagent-driven-development` like any code plan.
> **Track 2 (the maintenance-window node operations)** is a **human-executed
> runbook**: it reboots physical nodes, edits `/etc/rancher/k3s/`, and takes
> etcd snapshots, so Joe runs it by hand. Do **not** dispatch subagents to
> execute Track 2. This adapts `superpowers:writing-plans` for an ops migration:
> each task is action + verification command + expected output + rollback note,
> not TDD (there is no local test loop in this repo).

**Goal:** Replace the Linkerd sidecar mesh and k3s's embedded flannel CNI with
Cilium (eBPF CNI + native `CiliumNetworkPolicy` + transparent WireGuard
encryption), enabled cluster-wide, deleting the sidecar-injection machinery and
all opt-outs.

**Architecture:** Per ADR platform/012. flannel and Cilium cannot share the
datapath, so the swap is one coordinated maintenance window: stage all Git
changes first (Track 1), then in a single window flip k3s to hand networking to
Cilium, recreate every pod onto Cilium, drop the Linkerd sidecars, and apply the
translated policies (Track 2). Rollback is an etcd snapshot restore plus a k3s
config revert.

**Tech Stack:** k3s v1.35 (HA embedded etcd: node-1/2/3 servers, node-4 agent),
Cilium 1.19.x (Helm chart `oci://quay.io/cilium/charts/cilium`), WireGuard,
ArgoCD, `cilium-cli`, `hubble` (optional).

---

## Decisions locked for this plan

- **Cutover shape:** single coordinated window (Option A). The per-node
  `CiliumNodeConfig` migration (Option B) is rejected here: it assumes the old
  CNI is a standalone per-node DaemonSet, but k3s embeds flannel and toggles it
  with a cluster-level server flag, so per-node co-running fights k3s.
- **Encryption:** WireGuard, node-to-node (`encryption.nodeEncryption=true`).
- **kube-proxy:** keep k3s's kube-proxy (`kubeProxyReplacement=false`). Going
  kube-proxy-free is a later, independent Cilium config change.
- **Policy enforcement mode:** `default` (allow-until-selected), so tearing out
  Linkerd's `AuthorizationPolicy` objects causes no deny-all outage; policy is
  layered back on top afterward.
- **Hubble:** enabled but with a modest retention/flow buffer (observability
  parity with what linkerd-viz gave us). Can be disabled if memory pressure
  appears; it is the component that eats back the reclaimed RAM.
- **Cilium pod CIDR:** keep k3s flannel's default `10.42.0.0/16` for the Cilium
  cluster pool. Not relevant to a single-window swap (flannel is gone before
  Cilium IPAM runs), but keeping the range identical avoids re-plumbing any
  CIDR assumptions in policy.

---

## TRACK 1: Git / PR changes (staged ahead of the window)

All of Track 1 lands in ordinary PRs on green CI. Nothing here disrupts the
running cluster **except** the Cilium ArgoCD Application, which is created with
**automated sync disabled** so it does not try to install Cilium before the
window. Track 2 enables it.

### Task 1: Scaffold the Cilium platform component (bootstrap + ArgoCD app)

**Files:**
- Create: `projects/platform/cilium/application.yaml` (ArgoCD Application, chart
  `oci://quay.io/cilium/charts/cilium` version `1.19.x`, `syncPolicy` with
  automated **absent** so it is manual-sync until the window)
- Create: `projects/platform/cilium/values.yaml`
- Create: `projects/platform/cilium/kustomization.yaml` (`resources: [application.yaml]`)
- Create: `projects/platform/cilium/bootstrap/k3s-config.yaml` (the
  `flannel-backend: none` + `disable-network-policy: true` fragment, checked in
  as reference, applied by hand in Track 2)
- Create: `projects/platform/cilium/bootstrap/cilium-helmchart.yaml` (k3s
  `HelmChart` CR for `/var/lib/rancher/k3s/server/manifests/`, mirrors
  values.yaml, for the bootstrap install before ArgoCD adopts it)
- Create: `projects/platform/cilium/bootstrap/README.md` (traffic-shaper-style:
  "why this is applied out-of-band, not via ArgoCD", and the exact host paths)

**Step 1: Write `values.yaml`**

```yaml
# projects/platform/cilium/values.yaml
kubeProxyReplacement: false          # keep k3s kube-proxy (deferred item)
k8sServiceHost: 127.0.0.1            # k3s: reach apiserver during bootstrap
k8sServicePort: 6443

ipam:
  mode: cluster-pool
  operator:
    clusterPoolIPv4PodCIDRList: ["10.42.0.0/16"]  # match k3s flannel CIDR

encryption:
  enabled: true
  type: wireguard
  nodeEncryption: true               # encrypt node-to-node too

policyEnforcementMode: default        # allow-until-selected; no deny-gap at cutover

operator:
  replicas: 2

hubble:
  enabled: true
  relay:
    enabled: true
  # UI off; SigNoz remains the dashboard. Keep flow buffer modest.
  metrics:
    enabled: ["dns", "drop", "tcp", "flow"]
```

**Step 2: Write `application.yaml`** modeled on an existing platform app
(`projects/platform/coredns/application.yaml` is the closest single-source
template), pointing `chart: cilium`, `repoURL: quay.io/cilium/charts`,
`targetRevision: 1.19.x`, namespace `kube-system`, and **omit** `syncPolicy.automated`
so it will not self-install.

**Step 3: `format`** (regenerates the home-cluster root kustomization)

Run: `bazel/tools/format/fast-format.sh`
Expected: new app discovered, `projects/home-cluster/kustomization.yaml` updated.

**Step 4: Commit**

```bash
git add projects/platform/cilium projects/home-cluster
git commit -m "feat(cilium): add Cilium platform component (sync disabled until cutover)"
```

**Rollback:** delete the directory; nothing has synced.

---

### Task 2: Translate the monolith-public Linkerd policy to CiliumNetworkPolicy

**Files:**
- Create: `projects/monolith-public/chart/templates/cilium-policy.yaml`
- Delete: `projects/monolith-public/chart/templates/linkerd-policy.yaml` (do the
  delete in Track 2's teardown commit, not here, so the mesh keeps working until
  cutover)

**Step 1: Write the CiliumNetworkPolicy set**

Translate the 4 `Server` + `MeshTLSAuthentication` + `AuthorizationPolicy` chain
(gateway -> frontend -> backend) into label-selected `CiliumNetworkPolicy`
ingress rules, and the `EgressNetwork` deny-all + `TLSRoute` Turnstile allow into
a default-deny egress with a `toFQDNs` allow. Sketch:

```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: monolith-public-frontend
spec:
  endpointSelector:
    matchLabels: { app: monolith-public-web }   # confirm real labels
  ingress:
    - fromEndpoints:
        - matchLabels:
            k8s:io.kubernetes.pod.namespace: envoy-gateway-system
      toPorts:
        - ports: [{ port: "<web-port>", protocol: TCP }]
  egress:
    - toEndpoints: [{}]                          # in-cluster
    - toFQDNs:
        - matchName: "challenges.cloudflare.com"
      toPorts:
        - ports: [{ port: "443", protocol: TCP }]
    - toEndpoints:                               # allow DNS for toFQDNs
        - matchLabels:
            k8s:io.kubernetes.pod.namespace: kube-system
            k8s-app: kube-dns
      toPorts:
        - ports: [{ port: "53", protocol: ANY }]
          rules: { dns: [{ matchPattern: "*" }] }
```

> **NOTE:** verify the real pod labels and ports from the rendered chart
> (`helm template monolith-public projects/monolith-public/chart -f
> projects/monolith-public/deploy/values.yaml`) before finalizing. `toFQDNs`
> requires the DNS egress rule above or name resolution for the allow fails.

**Step 2: Bump the chart** (`bazel/tools/git/bump-chart.sh projects/monolith-public`)

**Step 3: Render to verify it templates**

Run: `helm template monolith-public projects/monolith-public/chart -f projects/monolith-public/deploy/values.yaml | grep -A30 CiliumNetworkPolicy`
Expected: valid CiliumNetworkPolicy objects, no template errors.

**Step 4: Commit**

```bash
git add projects/monolith-public
git commit -m "feat(monolith-public): CiliumNetworkPolicy replacing Linkerd policy"
```

**Rollback:** the file is inert until Cilium is the CNI and the chart is synced;
revert the commit.

---

### Task 3: Translate the firecracker/substrate /invoke policy

**Files:**
- Create: `projects/firecracker/substrate/chart/templates/cilium-policy.yaml`
- Delete (in Track 2 teardown): the `linkerd-policy.yaml`

**Step 1:** One `CiliumNetworkPolicy` selecting the substrate `/invoke` endpoint,
allowing ingress only from the monolith orchestrator's pod labels. This endpoint
is already TokenReview-gated in app code, so this policy is defense-in-depth.

**Step 2:** bump chart, render to verify, commit
`feat(substrate): CiliumNetworkPolicy for /invoke ingress`.

**Rollback:** revert commit; inert until cutover.

---

### Task 4: Stage the Linkerd teardown (do NOT merge-sync until Track 2)

**Files (a single teardown commit, cherry-picked/applied during the window):**
- Delete: `projects/platform/linkerd/` (whole component)
- Delete: `projects/platform/kyverno/templates/linkerd-injection-policy.yaml`
- Modify: remove the 16 `linkerd.io/inject: disabled` opt-outs across the repo
- Modify: remove `config.linkerd.io/skip-inbound-ports` (cnpg-cluster.yaml) and
  `skip-outbound-ports` (grimoire ws-gateway) annotations
- Modify: `projects/operators/oci-model-cache/internal/controller/job_builder.go`
  drop the `linkerd.io/inject: disabled` it stamps on generated Job pods
- Delete: the two `linkerd-policy.yaml` files (superseded by Tasks 2-3)

**Step 1:** Prepare this as its own commit on the branch but keep it as the
**last** thing synced. Sequencing rule from ADR 012: injection must be removed
**before** the mass pod-recreate so pods return sidecar-free, but the Linkerd
control-plane deletion happens **after** the recreate.

**Step 2:** `format`, render-check affected charts, commit
`refactor(linkerd): remove sidecar mesh, injection policy, and skip-port workarounds`.

**Rollback:** revert commit; re-sync restores Linkerd injection.

---

## TRACK 2: The maintenance window (human-executed runbook)

Pre-req: Track 1 merged; a maintenance window announced. Have a second terminal
and the `cilium` + `hubble` CLIs installed on your workstation.

### Task 5: Safety snapshot

**Step 1:** On a server node (node-1):

Run: `sudo k3s etcd-snapshot save --name pre-cilium`
Expected: snapshot path printed under `/var/lib/rancher/k3s/server/db/snapshots/`.

**Step 2:** Record current state for diffing later.

Run: `kubectl get pods -A -o wide > /tmp/pre-cilium-pods.txt && kubectl get ciliumnodes -A 2>/dev/null; echo "linkerd proxies:"; kubectl get pods -A -o json | jq '[.items[].spec.containers[]|select(.name=="linkerd-proxy")]|length'`
Expected: 38 linkerd proxies; no ciliumnodes yet.

**Rollback anchor:** this snapshot is the restore point for every later task.

---

### Task 6: Flip k3s to hand networking to Cilium

**Step 1:** On **node-1/2/3** append to `/etc/rancher/k3s/config.yaml`:

```yaml
flannel-backend: none
disable-network-policy: true
```

**Step 2:** Place the bootstrap Cilium install so k3s applies it on restart. On
node-1, copy `projects/platform/cilium/bootstrap/cilium-helmchart.yaml` to
`/var/lib/rancher/k3s/server/manifests/cilium.yaml`.

**Step 3:** Restart k3s so it comes up with no flannel. Servers first, then agent:

Run (each server): `sudo systemctl restart k3s`
Run (node-4): `sudo systemctl restart k3s-agent`
Expected: nodes `NotReady` briefly; new pods stuck `ContainerCreating` until
`cilium-agent` is up. This is expected, not a failure.

**Step 4:** Verify Cilium came up as CNI on all nodes.

Run: `cilium status --wait`
Expected: `cilium-agent` Ready on 4/4 nodes; `cilium-operator` running.

Run: `cilium connectivity test`
Expected: all tests pass (may take several minutes).

**Rollback:** remove the two lines from each `config.yaml`, delete
`/var/lib/rancher/k3s/server/manifests/cilium.yaml`, `systemctl restart k3s`;
if pod networking is wedged, restore the Task 5 snapshot
(`sudo k3s server --cluster-reset --cluster-reset-restore-path=<snapshot>`).

---

### Task 7: Remove Linkerd injection, then recreate all workloads onto Cilium

**Step 1:** Sync the Track 1 Task-4 teardown for the **injection** pieces only
(Kyverno injection policy + opt-outs), so recreated pods return sidecar-free.
Leave the `projects/platform/linkerd/` control-plane deletion for Task 8.

Run: `kubectl delete -f projects/platform/kyverno/templates/linkerd-injection-policy.yaml` (or let ArgoCD sync the kyverno app)
Expected: ClusterPolicy `inject-linkerd-namespace-annotation` gone.

**Step 2:** Recreate workloads so they get Cilium IPs and drop their proxies.
The Task 6 reboots already recreated most; force the rest, **stateless first**:

Run: `kubectl get deploy -A -o name | grep -vE 'cnpg|postgres|longhorn|seaweedfs' | xargs -I{} kubectl rollout restart {} -n <ns>`
Expected: pods return `Running`, 1/1 (no linkerd-proxy container).

**Step 3:** Recreate **stateful workloads last, one at a time** (CNPG Postgres,
Longhorn, SeaweedFS), watching each to `Ready` before the next.

Run: `kubectl get pods -A -o json | jq '[.items[].spec.containers[]|select(.name=="linkerd-proxy")]|length'`
Expected: `0` once every pod is recreated.

**Step 4:** Apply the translated policies (sync monolith-public + substrate
charts from Tasks 2-3).

Run: `kubectl get ciliumnetworkpolicies -A`
Expected: the monolith-public and substrate policies present.

**Rollback:** re-add the injection ClusterPolicy and re-sync Linkerd; restart
pods to re-acquire sidecars. Snapshot restore if networking is wedged.

---

### Task 8: Decommission Linkerd and verify end to end

**Step 1:** Sync the remaining Task-4 teardown: delete
`projects/platform/linkerd/` (control-plane + CRDs).

Run: `kubectl get ns linkerd; kubectl get crd | grep linkerd`
Expected: namespace terminating/gone; policy CRDs removed.

**Step 2:** Verify encryption is actually on the wire.

Run: `cilium encrypt status`
Expected: `Encryption: Wireguard`, non-zero peers/keys.

**Step 3:** Verify flows (if Hubble enabled).

Run: `hubble observe --namespace monolith-public --last 20`
Expected: live L3/L4 flows, no drops from the new policies.

**Step 4:** App-level health.

Run: `curl -s https://jomcgi.dev/health | jq .` and spot-check the public tier.
Expected: healthy; public pages load.

**Step 5:** Adopt Cilium into ArgoCD: enable `syncPolicy.automated` on the
Cilium Application (Task 1) so ongoing config is reconciled by GitOps. The
bootstrap manifest stays as the install seam per ADR 012.

**Rollback (full):** revert the Linkerd teardown commit, re-sync Linkerd, revert
k3s config, restart k3s, restore the Task 5 snapshot as last resort.

---

## Post-migration follow-ups (not this plan)

- Consider `kubeProxyReplacement=true` as an independent change once stable.
- Consider tightening `policyEnforcementMode` to `always` (default-deny) once
  `CiliumNetworkPolicy` coverage is audited complete.
- Update `docs/security.md` to record the trust-model change (WireGuard
  node-to-node encryption + label-based Cilium identity, replacing per-workload
  mTLS identity).
- Remove the "never K8s NetworkPolicies in meshed namespaces" caveat: native
  NetworkPolicy now works everywhere.

## References

| Resource | Relevance |
| -------- | --------- |
| ADR platform/012 | The decision this plan implements |
| https://docs.cilium.io/en/stable/installation/k8s-install-migration | Per-node migration (the rejected Option B) |
| https://docs.cilium.io/en/stable/security/network/encryption-wireguard | WireGuard Helm values (verified) |
| https://docs.cilium.io/en/stable/gettingstarted/k8s-install-default | `--flannel-backend=none --disable-network-policy` (verified) |
| `projects/platform/node-traffic-shaper/README.md` | Precedent for out-of-band node config |
