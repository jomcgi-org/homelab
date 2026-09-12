#!/usr/bin/env bash
# Install the documented single-host profile into the current k3s context.
set -o errexit -o nounset -o pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root=$(cd -- "${script_dir}/../../.." && pwd)
chart_version=${EMBER_QUICKSTART_CHART_VERSION:-0.78.2}
namespace=embervm
release=embervm
temporary_dir=$(mktemp -d)
trap 'rm -rf -- "${temporary_dir}"' EXIT

"${script_dir}/host-check.sh"

mapfile -t nodes < <(kubectl get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}')
if ((${#nodes[@]} != 1)); then
	printf 'expected exactly one Kubernetes node, found %d\n' "${#nodes[@]}" >&2
	exit 1
fi
node=${nodes[0]}
kubectl wait "node/${node}" --for=condition=Ready --timeout=2m

kubectl create namespace "${namespace}" --dry-run=client -o yaml | kubectl apply -f -
kubectl label node "${node}" homelab.io/firecracker=true --overwrite

random_hex() {
	od -An -N "$1" -tx1 /dev/urandom | tr -d ' \n'
}

if ! kubectl -n "${namespace}" get secret embervm-store >/dev/null 2>&1; then
	store_user="ember$(random_hex 8)"
	store_password=$(random_hex 24)
	kubectl -n "${namespace}" create secret generic embervm-store \
		--from-literal=access_key_id="${store_user}" \
		--from-literal=secret_access_key="${store_password}"
fi

if ! kubectl -n "${namespace}" get secret embervm-noded-token >/dev/null 2>&1; then
	noded_token=$(random_hex 32)
	kubectl -n "${namespace}" create secret generic embervm-noded-token \
		--from-literal=token="${noded_token}"
fi

continuity_zip="${temporary_dir}/continuity.zip"
continuity_sha=$(python3 "${script_dir}/build-fixtures.py" --check --output "${continuity_zip}")
hello_zip="${repo_root}/projects/embervm/runtimes/python/testdata/echo/echo.zip"
hello_sha=53ff98ccb09d4d12a629322caac8ee0aee9f77ca69fd08fbc1eee83b7a60230b
if [[ $(sha256sum "${hello_zip}" | awk '{print $1}') != "${hello_sha}" ]]; then
	printf 'hello fixture checksum does not match %s\n' "${hello_sha}" >&2
	exit 1
fi
if [[ "${continuity_sha}" != bd12054be9fc385984157d27bba1847ab2ceb4a8167db26b3d0c0b99784f31d9 ]]; then
	printf 'continuity fixture checksum does not match the Workload manifest\n' >&2
	exit 1
fi

kubectl -n "${namespace}" create configmap embervm-quickstart-functions \
	--from-file=hello.zip="${hello_zip}" \
	--from-file=continuity.zip="${continuity_zip}" \
	--dry-run=client -o yaml | kubectl apply -f -

kubectl -n "${namespace}" apply -f "${script_dir}/platform.yaml"
kubectl -n "${namespace}" rollout status deployment/embervm-minio --timeout=5m
kubectl -n "${namespace}" delete job embervm-fixture-upload --ignore-not-found --wait=true
kubectl -n "${namespace}" apply -f "${script_dir}/upload-job.yaml"
kubectl -n "${namespace}" wait job/embervm-fixture-upload --for=condition=complete --timeout=5m

helm upgrade --install "${release}" oci://ghcr.io/jomcgi/homelab/charts/embervm \
	--namespace "${namespace}" \
	--version "${chart_version}" \
	--values "${script_dir}/values.yaml"

kubectl wait crd/workloads.embervm.dev --for=condition=Established --timeout=2m
kubectl -n "${namespace}" rollout status daemonset/embervm-embervm-scratch-prep --timeout=10m
kubectl -n "${namespace}" rollout status deployment/embervm-embervm --timeout=10m
kubectl -n "${namespace}" rollout status daemonset/embervm-embervm-noded --timeout=15m

kubectl -n "${namespace}" apply -f "${script_dir}/workloads/hello.yaml"
kubectl -n "${namespace}" apply -f "${script_dir}/workloads/continuity.yaml"
kubectl -n "${namespace}" wait workload/hello --for=condition=Ready --timeout=10m
kubectl -n "${namespace}" wait workload/continuity --for=condition=Ready --timeout=10m

printf 'EmberVM standalone profile is ready on node %s with chart %s.\n' "${node}" "${chart_version}"
printf 'Continue with the ordinary workload walkthrough in projects/embervm/QUICKSTART.md.\n'
