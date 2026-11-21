#!/usr/bin/env bash
set -euo pipefail

[[ "$(id -u)" == 0 && "$(hostname -s)" == "node-1" ]] || {
	echo "run as root on node-1" >&2
	exit 2
}
if [[ "$#" != 1 || ("$1" != "--stage" && "$1" != "--activate") ]]; then
	echo "usage: install-t3-socket.sh --stage|--activate" >&2
	exit 2
fi
if [[ "$1" == "--activate" ]]; then
	primary_users=$(getent passwd | awk -F: '$4 == 65532 { print $1 }')
	if [[ -n "$primary_users" ]]; then
		echo "T3 socket group 65532 already has a host account" >&2
		exit 1
	fi
	if ! getent group t3-cluster >/dev/null; then
		groupadd --system --gid 65532 t3-cluster
	fi
	group=$(getent group t3-cluster)
	IFS=: read -r _ _ gid members <<<"$group"
	groups=$(getent group | awk -F: '$3 == 65532 { print $1 }')
	if [[ "$gid" != 65532 || -n "$members" || "$groups" != "t3-cluster" ]]; then
		echo "expected the dedicated T3 socket group at GID 65532 with no members" >&2
		exit 1
	fi
fi
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
install -m 0644 "${here}"/t3-cluster.{socket,service} /etc/systemd/system/
systemctl daemon-reload
if [[ "$1" == "--activate" ]]; then
	systemctl enable --now t3-cluster.socket
fi
