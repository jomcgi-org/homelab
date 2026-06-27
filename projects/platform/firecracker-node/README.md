# Firecracker node setup (node-4)

Host-level setup that makes the `kata-fc` RuntimeClass (Firecracker microVMs)
work on **node-4**. These scripts are **not** applied by ArgoCD: like
`node-traffic-shaper`, they are version-controlled for reproducibility but run on
the host with `sudo`. The cluster-side object (the `kata-fc` RuntimeClass) lives
in `projects/platform/kata-fc/` and is GitOps-managed.

Context and rationale: ADR `docs/decisions/platform/010` (memory oversubscription
and the agent-microVM tier) and ADRs `agents/019` / `agents/021`.

## Why node-4 only

node-4 is the lone non-control-plane worker (AMD, `/dev/kvm` present). Snapshots
are node/ISA-bound (AMD `svm` vs the Intel `vmx` control-plane nodes are not
portable), and microVMs must not contend with etcd on the 15 GiB control-plane
boxes. The RuntimeClass pins `kata-fc` pods to node-4 via `nodeSelector`.

## What gets installed

- **devmapper thin-pool (`devpool`)**: the block-device snapshots Firecracker
  needs (overlayfs cannot back a microVM rootfs). Recreated on boot by
  `containerd-devpool.service` from sparse backing files under
  `/disks/nvme-02/containerd-devmapper/`.
- **Kata Containers + Firecracker**: installed to `/opt/kata` (shim,
  `firecracker`, guest kernel + rootfs). Plain files on disk.
- **containerd config**: the devmapper snapshotter plus the `kata-fc` runtime
  handler, via `config.toml.tmpl` in the k3s `data-dir`
  (`/disks/nvme-01/k3s-data/agent/etc/containerd/`); k3s re-renders `config.toml`
  from it on every start.

Note the k3s `data-dir` override (`/disks/nvme-01/k3s-data`, set in
`/etc/rancher/k3s/config.yaml`): the active containerd config is under that tree,
**not** the default `/var/lib/rancher/k3s`.

## Apply order (on node-4, as root)

```bash
sudo bash setup-devpool.sh          # 1. create the devmapper thin-pool (non-disruptive)
sudo install -m755 devpool-up.sh /usr/local/sbin/devpool-up.sh
sudo install -m644 containerd-devpool.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now containerd-devpool.service  # 2. boot-persistence
sudo bash install-kata.sh           # 3. install Kata + Firecracker to /opt/kata (non-disruptive)
sudo bash fix-kata-containerd.sh    # 4. wire containerd + restart k3s-agent (DISRUPTIVE; auto-rollback)
```

Step 4 restarts k3s-agent (brief node-4 perturbation) and self-reverts if the
node does not come back healthy. After it succeeds, `k3s ctr plugins ls` shows
the `devmapper` snapshotter `ok` and the rendered `config.toml` carries the
`kata-fc` runtime.

## Verify

```bash
# containerd-level (no k8s): boots a real Firecracker microVM
sudo bash test-kata-fc.sh           # guest kernel != host 6.8.x means a microVM booted
```

Or via Kubernetes once the `kata-fc` RuntimeClass is deployed: the
`kata-fc-smoke` Job in `projects/platform/kata-fc/` prints the guest kernel.

## Rollback

```bash
rm /disks/nvme-01/k3s-data/agent/etc/containerd/config.toml.tmpl
sudo systemctl restart k3s-agent    # k3s regenerates its default config
```
