# node-traffic-shaper

> ## STATUS: INERT. DO NOT TREAT THIS AS A CONTROL.
>
> Measurements recorded in [#4171](https://github.com/jomcgi/homelab/issues/4171)
> on 2026-07-30 showed this CAKE/IFB shaper shaping **nothing** on the home
> cluster. Cilium attaches to the uplink with `tcx` (BPF-link TC, kernel 6.8),
> which runs **before** legacy tc filters and is invisible to `tc qdisc show` /
> `tc filter show`. `cil_from_netdev` consumes the packet, so the legacy
> `ingress` qdisc and its `mirred` redirect never see traffic.
>
> What that issue measured, applying the script cleanly to node-3 and reading
> node-4 alongside it:
>
> | Check                                       | Result                            |
> | ------------------------------------------- | --------------------------------- |
> | node-3 `mirred` action after 193s installed | `Sent 0 bytes 0 pkt`              |
> | node-3 `enp2s0` NIC rx over the same window | +26.5 MB in 20s                   |
> | node-4 `ifb0` delta over 25s                | +0 bytes                          |
> | node-4 ingress qdisc                        | absent entirely (only `fq_codel`) |
>
> **Old cumulative IFB counts do not prove liveness.** node-4's 8.6 TB / 6.5 G
> packet counter on `ifb0` is entirely pre-migration history and has not moved
> in months. A dead shaper and a working one look identical in
> `tc -s qdisc show dev ifb0`. Only a **delta** over an interval, or the
> `mirred` action's own `Sent`, distinguishes them.
>
> Everything below the banner is kept as the historical record of the original
> design and of what is installed on node-4. It is **not** an active protection
> claim and **not** a rollout recommendation. Nothing in this directory is
> deployed by Bazel, Helm, or ArgoCD; the only instance that ever existed is a
> hand-installed systemd unit on node-4.

## Why it was built (historical)

On the AI node, pulling model weights saturates the 1GbE uplink. Two sources:

1. **Container/image-volume pulls** done by `containerd` (the `oci-model-cache`
   operator delivers weights as OCI image volumes). These land in the **host**
   network namespace, so they cannot be shaped by per-pod CNI bandwidth
   annotations or by any in-pod limiter.
2. The `hf2oci` copy Job's download from HuggingFace.

The reasoning at the time was that the only enforcement point catching **both**
is the physical uplink, and that `containerd` has no bytes/sec download limit,
so shaping had to happen at the NIC with `tc`. The intended effect was CAKE's
per-flow fairness keeping etcd / kubelet / SSH in their share during a
saturating pull. Per the 2026-07-30 evidence above, that effect was never
delivered on this cluster: the packets never reached the qdisc.

Two further caveats on the original premise, both from #4171: per-node shaping
never coordinated across nodes, so four nodes at 940mbit still oversubscribe a
shared WAN, and #4169 removed the rollout stampede that was the actual source
of the saturating pulls.

## Why node-local (historical)

Only the AI node pulled large images, so cluster-wide fan-out looked
unnecessary, and a node-local systemd unit gave reboot persistence without the
apko-image plus Helm chart plus ArgoCD machinery a DaemonSet would need. The
consequence is that ArgoCD has no view of it: there is nothing to reconcile,
nothing to sync, and removal is an SSH operation rather than a Git one.

The earlier advice here to promote this to a privileged DaemonSet if other
nodes started pulling weights is **withdrawn**. Fanning out a shaper that the
tcx datapath bypasses would multiply the inert install, not the protection.

## Files

| File                          | Installed to           | Purpose                                                    |
| ----------------------------- | ---------------------- | ---------------------------------------------------------- |
| `node-traffic-shaper.sh`      | `/usr/local/sbin/`     | Idempotent apply (ifb redirect + CAKE). Inert under `tcx`. |
| `node-traffic-shaper-down.sh` | `/usr/local/sbin/`     | Teardown / revert. Still the correct uninstall path.       |
| `node-traffic-shaper.service` | `/etc/systemd/system/` | Re-applies the inert config on every boot.                 |

The apply script auto-detects the default-route interface, so it is correct on
any node regardless of NIC name (`enp1s0` / `enp2s0` / `enp12s0` across this
cluster). The ceiling comes from `BANDWIDTH=` (default `940mbit`), set in the
unit. Both remain accurate descriptions of what the files do; neither changes
the fact that the datapath never reaches them.

## Historical install (recorded, not recommended)

node-4 carries the install that #4171 found inert. It was staged by hand: the
two scripts to `/usr/local/sbin/`, the unit to `/etc/systemd/system/`, a
`systemctl daemon-reload`, then `systemctl enable --now node-traffic-shaper`.
Do not repeat this on another node. The step that made the install look healthy
was reading `tc -s qdisc show dev ifb0` and seeing a large cumulative counter,
which is exactly the check that cannot detect the failure.

## Decommission on node-4: PENDING

**This PR does not perform the retirement.** It is documentation only: no
cluster contact, no SSH, no `systemctl`, and no change to the scripts' or the
unit's behaviour. The unit on node-4 is still enabled and still re-applies its
inert configuration on every boot.

Retiring it needs interactive sudo on node-4 and is Joe's to run. Grounded in
the files in this directory, the operation is the `Remove` block below, which
runs `node-traffic-shaper-down.sh` by way of `ExecStop` in
`node-traffic-shaper.service` and then deletes all three installed paths.
Node-4 is also in scope for the home-cluster teardown
([#5485](https://github.com/jomcgi/homelab/issues/5485)), so the retirement may
land there instead of on its own.

Because nothing here is under GitOps, deleting this directory from the repo
would **not** stop the unit. The node-side step is independent of the repo-side
one, and only the repo-side one is in this PR.

## Remove (the decommission procedure, run on node-4 with sudo)

```bash
sudo systemctl disable --now node-traffic-shaper
sudo rm -f /etc/systemd/system/node-traffic-shaper.service \
           /usr/local/sbin/node-traffic-shaper.sh \
           /usr/local/sbin/node-traffic-shaper-down.sh
sudo systemctl daemon-reload
```

## Open question: whether and where replacement shaping belongs

**Joe owns this decision. It is not settled by this document and not settled by
this PR.** #4171 tracks it, and ARCHITECTURE.md section 6 records the current
state. The candidates it names:

- **The router or gateway.** Upstream of the tcx hook, so Cilium cannot bypass
  it, and shared by all four nodes, so it enforces one WAN ceiling instead of
  four independent ones. Nothing about it lands in this repo beyond a runbook.
- **Cilium's bandwidth manager**, which lives in the datapath that actually
  runs. It is not enabled in `projects/platform/cilium/`. Note that its
  `kubernetes.io/egress-bandwidth` implementation is pod egress, which is not
  the node-ingress, host-namespace `containerd` traffic this directory was
  built for.
- **Nothing at all**, given #4169 removed the stampede that motivated the
  shaper.

Whatever is chosen, if anything, must carry a liveness check that would have
caught this failure:

- Assert on a **byte delta over an interval**, or on the `mirred` action's own
  `Sent`, or on an equivalent action counter in whichever datapath does the
  work.
- Never assert on a cumulative `tc -s qdisc` total. That is precisely the
  reading that made node-4 look protected for months while it shaped zero
  bytes.

Selecting or implementing a replacement, and enabling the bandwidth manager,
are explicitly out of scope here.
