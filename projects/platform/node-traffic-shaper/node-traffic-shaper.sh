#!/usr/bin/env bash
# node-traffic-shaper: cap inbound bandwidth on a node's uplink with CAKE so a
# bulk download (model-weight image pulls, hf2oci) cannot starve latency-sensitive
# control-plane traffic (etcd, kubelet, SSH).
#
# INERT ON THE HOME CLUSTER. Measurements on 2026-07-30 recorded in #4171 found
# this script shapes nothing there: Cilium attaches to the uplink with tcx
# (BPF-link TC, kernel 6.8), which runs before legacy tc filters, so
# cil_from_netdev consumes the packet and the mirred redirect below never sees
# it. The mirred action reported `Sent 0 bytes 0 pkt` after 193s installed while
# the NIC took +26.5 MB in 20s. A large cumulative `tc -s qdisc show dev ifb0`
# counter does NOT prove otherwise: only a delta, or the mirred action's own
# Sent, does. See projects/platform/node-traffic-shaper/README.md. The code
# below is unchanged and still does what it says; the datapath just never
# reaches it.
#
# Ingress cannot be shaped directly, so we redirect the uplink's ingress onto an
# intermediate functional block (ifb0) and run CAKE on ifb0's egress. CAKE's
# per-flow fairness is what protects control traffic during a saturating pull.
#
# Idempotent: safe to run repeatedly and on every boot. Auto-detects the
# default-route interface so it works regardless of NIC name.
set -euo pipefail

BANDWIDTH="${BANDWIDTH:-940mbit}"
IFB="ifb0"

IFACE="$(ip route show default | awk '/default/{print $5; exit}')"
if [ -z "${IFACE:-}" ]; then
	echo "node-traffic-shaper: no default-route interface found" >&2
	exit 1
fi

# Load modules (autoload via the ip/tc calls below covers the no-op case).
modprobe ifb numifbs=1 2>/dev/null || true
modprobe sch_cake 2>/dev/null || true

# Intermediate functional block device, brought up once.
if ! ip link show "$IFB" >/dev/null 2>&1; then
	ip link add "$IFB" type ifb
fi
ip link set dev "$IFB" up

# Redirect all uplink ingress onto ifb0. Clean del+add so reconciles never
# accumulate duplicate redirect filters.
tc qdisc del dev "$IFACE" ingress 2>/dev/null || true
tc qdisc add dev "$IFACE" handle ffff: ingress
tc filter add dev "$IFACE" parent ffff: protocol all u32 \
	match u32 0 0 action mirred egress redirect dev "$IFB"

# The actual shaper: CAKE on the redirected ingress, in ingress-accounting mode.
tc qdisc replace dev "$IFB" root cake bandwidth "$BANDWIDTH" ingress

echo "node-traffic-shaper: shaping ${IFACE} ingress -> ${IFB} @ ${BANDWIDTH}"
