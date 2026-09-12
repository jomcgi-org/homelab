#!/usr/bin/env bash
# Remove quickstart Kubernetes resources while retaining host scratch for reuse.
set -o errexit -o nounset -o pipefail

namespace=embervm
release=embervm

kubectl -n "${namespace}" delete workload hello continuity --ignore-not-found --wait=true
helm uninstall "${release}" --namespace "${namespace}" --wait
kubectl delete namespace "${namespace}" --wait
kubectl delete priorityclass embervm-standalone --ignore-not-found

mapfile -t nodes < <(kubectl get nodes -l homelab.io/firecracker=true -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
if ((${#nodes[@]} == 1)); then
	kubectl label node "${nodes[0]}" homelab.io/firecracker-
fi

printf '%s\n' 'Kubernetes resources were removed.'
printf '%s\n' 'The /var/lib/embervm/scratch mount, backing image, and fstab entry were retained.'
printf '%s\n' 'See QUICKSTART.md for the inspected, optional host-storage purge.'
