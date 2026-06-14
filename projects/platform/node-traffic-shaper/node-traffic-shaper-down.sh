#!/usr/bin/env bash
# Tear down the shaping installed by node-traffic-shaper.sh. Used as the systemd
# ExecStop and available for manual revert. Best-effort: never fails.
set -uo pipefail

IFB="ifb0"
IFACE="$(ip route show default | awk '/default/{print $5; exit}')"

[ -n "${IFACE:-}" ] && tc qdisc del dev "$IFACE" ingress 2>/dev/null || true
tc qdisc del dev "$IFB" root 2>/dev/null || true
ip link set dev "$IFB" down 2>/dev/null || true

echo "node-traffic-shaper: removed shaping"
