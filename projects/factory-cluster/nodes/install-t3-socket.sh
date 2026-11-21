#!/usr/bin/env bash
set -euo pipefail

[[ "$(id -u)" == 0 && "$(hostname -s)" == "node-1" ]] || {
	echo "run as root on node-1" >&2
	exit 2
}
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
install -m 0644 "${here}"/t3-cluster.{socket,service} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now t3-cluster.socket
