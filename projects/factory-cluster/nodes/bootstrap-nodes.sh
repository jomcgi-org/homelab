#!/usr/bin/env bash
# Bring up the three-server factory k3s cluster from an operator machine.
#
# Needs SSH with passwordless sudo to each node. Copies this directory to each
# node, runs install-node.sh (init on the first, join on the rest) and writes
# an admin kubeconfig to ${KUBECONFIG_OUT:-$HOME/.kube/factory.yaml}.
# Idempotent: rerun to apply a configuration or version change, one node at a
# time.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIRST="${FIRST:-node-1}"
FIRST_IP="${FIRST_IP:-192.168.4.80}"
read -r -a JOINERS <<<"${JOINERS:-node-2 node-3}"
KUBECONFIG_OUT="${KUBECONFIG_OUT:-$HOME/.kube/factory.yaml}"

run_install() {
	local node="$1"
	shift
	ssh -n "${node}" 'rm -rf /tmp/factory-node && mkdir -p /tmp/factory-node'
	scp -q "${HERE}"/{install-node.sh,k3s-config.yaml,registries.yaml,containerd-runsc.toml,99-factory.conf} \
		"${node}:/tmp/factory-node/"
	ssh "${node}" "$@"
	ssh -n "${node}" 'rm -rf /tmp/factory-node'
}

# Nodes register without a CNI and stay NotReady until bootstrap-gitops.sh
# installs Cilium, so wait for registration only.
wait_registered() {
	local node="$1"
	for _ in $(seq 60); do
		if ssh "${FIRST}" "sudo k3s kubectl get node ${node} --no-headers 2>/dev/null" | grep -q .; then
			return 0
		fi
		sleep 5
	done
	echo "${node} did not register" >&2
	return 1
}

run_install "${FIRST}" 'sudo bash /tmp/factory-node/install-node.sh init'
wait_registered "${FIRST}"

token="$(ssh "${FIRST}" 'sudo cat /var/lib/rancher/k3s/server/token')"
for node in "${JOINERS[@]}"; do
	# The token goes over stdin: on the command line it would reach sudo's log
	# and the node's process list.
	run_install "${node}" \
		"sudo bash -c 'read -r K3S_TOKEN && export K3S_TOKEN && bash /tmp/factory-node/install-node.sh join https://${FIRST_IP}:6443'" \
		<<<"${token}"
	wait_registered "${node}"
done

mkdir -p "$(dirname "${KUBECONFIG_OUT}")"
ssh "${FIRST}" 'sudo cat /etc/rancher/k3s/k3s.yaml' |
	sed -e "s#https://127.0.0.1:6443#https://${FIRST_IP}:6443#" -e 's/: default$/: factory/' \
		>"${KUBECONFIG_OUT}"
chmod 0600 "${KUBECONFIG_OUT}"
KUBECONFIG="${KUBECONFIG_OUT}" kubectl get nodes -o wide
