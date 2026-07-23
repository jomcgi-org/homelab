# cilium/bootstrap

Out-of-band node configuration applied **by hand during the Cilium cutover
window** (Track 2), not through ArgoCD. These two files switch the cluster's
datapath from k3s flannel to Cilium.

## Why this exists (and why not GitOps)

flannel and Cilium cannot share the datapath. Swapping them requires:

1. A **k3s server-flag change** (`flannel-backend: none`,
   `disable-network-policy: true`) that only takes effect on k3s restart, and
2. A **node reboot** to tear down the old flannel interfaces and bring the node
   up with Cilium owning networking.

ArgoCD reconciles Kubernetes objects; it cannot edit a node's
`/etc/rancher/k3s/config.yaml`, restart k3s, or reboot a host. And there is a
chicken-and-egg problem at bootstrap: with flannel removed, the cluster has no
CNI until Cilium is installed, so Cilium cannot be delivered by the very control
plane that needs it running. k3s solves this with its auto-deploying manifests
directory, which is why the Cilium install is seeded there by hand rather than
by ArgoCD. Once the node is up and Cilium owns the datapath, the manual-sync
`../application.yaml` adopts the release and GitOps takes over from there.

## Files

| File                    | Installed to                                              | Applied on   | Purpose                                            |
| ----------------------- | --------------------------------------------------------- | ------------ | -------------------------------------------------- |
| `k3s-config.yaml`       | append to `/etc/rancher/k3s/config.yaml`                  | node-1/2/3   | disable flannel + k3s network-policy               |
| `cilium-helmchart.yaml` | `/var/lib/rancher/k3s/server/manifests/cilium.yaml`       | node-1 only  | k3s auto-installs Cilium at bootstrap (CNI seed)   |

## Apply (interactive sudo, during the maintenance window)

Per the full cilium migration runbook (Track 2):

1. **Every server node (node-1, node-2, node-3):** append the flannel-disable
   fragment to the k3s config:

   ```bash
   sudo tee -a /etc/rancher/k3s/config.yaml < k3s-config.yaml
   ```

2. **node-1 only:** seed the Cilium install into the k3s auto-manifests
   directory so k3s installs it during the restart:

   ```bash
   sudo install -m 0644 cilium-helmchart.yaml \
     /var/lib/rancher/k3s/server/manifests/cilium.yaml
   ```

3. Restart k3s / reboot each node per the Track 2 runbook, then verify Cilium is
   healthy before adopting the ArgoCD Application.

`cilium-helmchart.yaml` pins the **same** chart version (1.19.5) and the same
values as `../values.yaml`, so the bootstrap install and the ArgoCD-managed
release converge on identical config. Keep the two in sync if either changes.

**One deliberate exception:** the Hubble TLS method. `../values.yaml` uses
`hubble.tls.auto.method=certmanager`; this seed stays on the upstream default
(`helm`). cert-manager and its CRDs are not installed yet at a cold cluster
bootstrap, so a `kind: Certificate` here would fail to apply and, with
`failurePolicy: abort`, leave the cluster with no CNI. ArgoCD reissues the
Hubble certs through cert-manager when it adopts the release, so the two
converge in steady state without the seed depending on cert-manager.

Note the two pull from **different registries on purpose**: this bootstrap CR
uses the classic Helm repo `https://helm.cilium.io` (what the k3s `HelmChart`
CR's `repo:` field expects), while `../application.yaml` / `../Chart.lock` pin
the OCI chart `oci://quay.io/cilium/charts` (digest-locked). Both resolve to
upstream Cilium 1.19.5; the version, not a shared digest, is the contract. The
bootstrap is a one-time CNI seed that the ArgoCD release adopts, after which the
OCI-pinned source is authoritative.

## Rollback

Rollback is an etcd snapshot restore plus a k3s config revert (remove the
`flannel-backend` / `disable-network-policy` keys and the seeded manifest, then
reboot). The full rollback procedure is in the Track 2 runbook.
