# master-image-gc

Lowers kubelet's image garbage-collection thresholds on the k3s nodes so the
root filesystem stops creeping toward full on stale container images. This is
the R7 disk-pressure unblock for node-1 and node-3.

## Why this exists

node-1 and node-3 (etcd masters) sit at 80-81% root-fs. A prior storage audit
found the pressure is host containerd images / k3s state, not Longhorn or PVCs
(their Longhorn footprint is tiny: node-1 ~2.5 G, node-3 ~23 G). So no
`values.yaml` edit reaches it.

The image cache is the dominant, addressable consumer. The `node.status.images`
API list is capped at 50 entries, so it undercounts badly (it showed only ~21
GiB); the true containerd image store, measured from the kubelet stats-summary
endpoint (`/api/v1/nodes/<node>/proxy/stats/summary`, `runtime.imageFs.usedBytes`),
is **~135 GiB on node-1 and ~131 GiB on node-3**, i.e. the bulk of the ~189 GiB
used. It is stale app images: every monolith CI push produces a new date-stamped
`monolith/backend` tag, the masters are **untainted** and run monolith /
monolith-public / embervm / signoz pods, so every rollout pulls the new image
onto whichever master it lands on, and the old tags and their layers are never
evicted. (Masters stay untainted deliberately: ADR embervm/011 and 012 put bricks
on all four nodes including the etcd masters, so they are wanted as capacity;
kubelet image GC, not a taint, is the right ongoing control.)

They are never evicted because kubelet's default `imageGCHighThresholdPercent`
is **85**, and these nodes sit at 80-81%, just under it. Image GC only runs above
the high mark, and even then only claws back to the low mark (default 80). So the
cache grows monotonically and nothing reclaims it.

Lowering the high threshold to **70** (low **55**) makes kubelet evict any image
not backing a running or recently-stopped container, continuously, and hold the
node there. `image-minimum-gc-age=2h` keeps an image pulled by an in-flight
rollout from being reaped before its pod is Ready.

### Scope and expected reclaim

The stats-summary measurement changes the picture from the first-pass estimate:
the containerd image store (~135 GiB on node-1, ~131 GiB on node-3) IS the
dominant term, not invisible non-image state. Measured pod logs and ephemeral
storage are negligible (< 0.2 GiB per node). So lowering the GC marks targets the
right thing: kubelet reclaims the large unused portion of that image store (the
stale monolith tags and orphaned layers back no running container), which should
drop the root-fs well below the 80% pressure line and, more importantly, hold it
there instead of climbing monotonically. The residual non-image k3s state (etcd
data + snapshots, `/var/lib/rancher/k3s/agent`, journald) is minor by comparison
but still worth a one-time spot-check (see the inspection block below) in case
journald or etcd retention is a secondary term.

## Why host-level (not a DaemonSet / GitOps)

k3s is host-provisioned. Its kubelet flags live in `/etc/rancher/k3s/config.yaml`
on each node, the same file the Cilium cutover edits by hand (see
`../cilium/bootstrap/`). ArgoCD reconciles Kubernetes objects; it cannot edit a
node's k3s config or restart k3s. So the durable fix is a config fragment applied
host-side, following the same reference-files + README convention as
`../node-traffic-shaper/` and `../cilium/bootstrap/`.

A privileged image-prune DaemonSet running `crictl rmi --prune` was the GitOps
alternative. It was rejected: it adds a privileged workload mounting the host
containerd socket onto every master to duplicate what kubelet already does
natively once its threshold is correct. Native kubelet GC is strictly simpler and
has no new attack surface. If k3s ever stops honoring `kubelet-arg` for GC (it
does today), revisit the DaemonSet.

## Files

| File                | Installed to                            | Applied on          | Purpose                                 |
| ------------------- | --------------------------------------- | ------------------- | --------------------------------------- |
| `k3s-image-gc.yaml` | append to `/etc/rancher/k3s/config.yaml`| node-1/2/3 (node-4) | lower kubelet image-GC high/low marks   |

## Apply (interactive sudo, one node at a time)

Do the masters one at a time and confirm etcd stays healthy between each, since a
k3s restart briefly bounces the API server on that node.

1. Stage the fragment to the node, then append it to the k3s config:

   ```bash
   # inspect the current config first; if it already has a kubelet-arg: block,
   # MERGE these three list entries into it rather than adding a second key
   # (a duplicate top-level kubelet-arg key makes k3s take only the last one).
   sudo cat /etc/rancher/k3s/config.yaml

   sudo tee -a /etc/rancher/k3s/config.yaml < k3s-image-gc.yaml
   ```

2. Restart k3s so kubelet picks up the new flags:

   ```bash
   # masters run the server unit:
   sudo systemctl restart k3s
   # node-4 (agent) runs:
   # sudo systemctl restart k3s-agent
   ```

3. Verify the flags took and GC is reclaiming:

   ```bash
   # kubelet should now show the lowered thresholds in its process args:
   ps -ww -C kubelet -o args= | tr ' ' '\n' | grep image-gc
   # expect: --image-gc-high-threshold=70 --image-gc-low-threshold=55

   # watch the image cache shrink over a few minutes:
   sudo k3s crictl images | wc -l
   df -h /
   ```

   From a workstation, confirm the node stayed healthy and un-pressured:

   ```bash
   kubectl get node node-1 -o jsonpath='{range .status.conditions[?(@.type=="DiskPressure")]}{.status}{end}'
   kubectl describe node node-1 | grep -A2 'Allocated resources'
   ```

Repeat for node-2, node-3, and (optionally) node-4.

## Owner: spot-check the residual non-image state

The image store (~135 GiB) is the bulk and the GC change handles it; this block
confirms the smaller non-image remainder (etcd, journald, containerd metadata)
is not a secondary problem. After the GC change lands, on each master run:

```bash
sudo du -xh --max-depth=1 /var/lib/rancher/k3s | sort -h | tail
sudo du -xh --max-depth=1 /var/lib/rancher/k3s/agent/containerd | sort -h | tail
sudo journalctl --disk-usage
sudo du -sh /var/log/pods 2>/dev/null
```

Likely large, non-image contributors and their durable remedies:

- **journald** with no cap: `sudo journalctl --vacuum-size=500M` once, then set
  `SystemMaxUse=1G` in `/etc/systemd/journald.conf`.
- **etcd on-disk data + snapshots** under `/var/lib/rancher/k3s/server/db`: check
  `etcd-snapshot-retention` and the snapshot dir size; k3s keeps 5 by default.
- **orphaned containerd snapshot layers**: `sudo k3s crictl rmi --prune` clears
  unused images immediately (kubelet does this on its own schedule after the GC
  change; this is the manual one-shot).

## Rollback

Remove the three `kubelet-arg` entries from `/etc/rancher/k3s/config.yaml`
(or drop the whole block if it was added standalone) and restart k3s. kubelet
reverts to the 85/80 defaults; the cache regrows.
