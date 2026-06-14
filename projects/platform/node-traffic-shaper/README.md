# node-traffic-shaper

Caps **inbound** bandwidth on a node's uplink with [CAKE](https://www.bufferbloat.net/projects/codel/wiki/Cake/)
so a bulk download cannot starve latency-sensitive control-plane traffic.

## Why this exists

On the AI node, pulling model weights saturates the 1GbE uplink. Two sources:

1. **Container/image-volume pulls** done by `containerd` (the `oci-model-cache`
   operator delivers weights as OCI image volumes). These land in the **host**
   network namespace, so they cannot be shaped by per-pod CNI bandwidth
   annotations or by any in-pod limiter.
2. The `hf2oci` copy Job's download from HuggingFace.

The only enforcement point that catches **both** (plus any future workload) is
the physical uplink. `containerd` has no bytes/sec download limit, so shaping has
to happen at the NIC with `tc`. CAKE's per-flow fairness is the part that matters:
during a saturating pull, etcd / kubelet / SSH keep their share instead of
grinding to a halt.

## Why node-local (not a DaemonSet / GitOps)

Only the AI node pulls large images, so cluster-wide fan-out is unnecessary. A
node-local systemd unit gives reboot persistence without the apko-image + Helm
chart + ArgoCD machinery a DaemonSet would need. Trade-off: if the node is
reprovisioned, reinstall the unit (see below). If other nodes start pulling
weights, promote this to a privileged DaemonSet (the uplink auto-detect,
`sch_cake`/`ifb` availability, and semgrep exclusions all carry over).

## Files

| File                          | Installed to           | Purpose                                |
| ----------------------------- | ---------------------- | -------------------------------------- |
| `node-traffic-shaper.sh`      | `/usr/local/sbin/`     | Idempotent apply (ifb redirect + CAKE) |
| `node-traffic-shaper-down.sh` | `/usr/local/sbin/`     | Teardown / revert                      |
| `node-traffic-shaper.service` | `/etc/systemd/system/` | Re-applies on every boot               |

The apply script auto-detects the default-route interface, so it is correct on
any node regardless of NIC name (`enp1s0` / `enp2s0` / `enp12s0` across this
cluster). Tune the ceiling with `BANDWIDTH=` (default `940mbit`).

## Install (interactive sudo required)

Stage the three files to the node, then:

```bash
sudo install -m 0755 /tmp/node-traffic-shaper.sh      /usr/local/sbin/node-traffic-shaper.sh
sudo install -m 0755 /tmp/node-traffic-shaper-down.sh /usr/local/sbin/node-traffic-shaper-down.sh
sudo install -m 0644 /tmp/node-traffic-shaper.service /etc/systemd/system/node-traffic-shaper.service
sudo systemctl daemon-reload
```

## First apply, safely (dead-man revert)

CAKE applied correctly does not blackhole traffic, but a bad `tc` change can lock
you out of SSH. Apply under a 120s auto-revert so a mistake self-heals. Run it
detached so it survives the SSH session dropping:

```bash
sudo nohup bash -c '
  /usr/local/sbin/node-traffic-shaper.sh
  sleep 120
  [ -f /tmp/keep-shaper ] || /usr/local/sbin/node-traffic-shaper-down.sh
' &>/tmp/shaper.log &
```

Confirm SSH still works and shaping is live, then lock it in:

```bash
tc -s qdisc show dev ifb0          # expect: cake ... bandwidth 940Mbit
sudo touch /tmp/keep-shaper        # cancels the auto-revert
sudo systemctl enable --now node-traffic-shaper   # persist across reboots
```

## Verify under load

While a model pull is running:

```bash
tc -s qdisc show dev ifb0          # drops/marks accrue on the bulk flow
```

Control-plane traffic (etcd, kubelet, SSH) should stay responsive.

## Remove

```bash
sudo systemctl disable --now node-traffic-shaper
sudo rm -f /etc/systemd/system/node-traffic-shaper.service \
           /usr/local/sbin/node-traffic-shaper.sh \
           /usr/local/sbin/node-traffic-shaper-down.sh
sudo systemctl daemon-reload
```
