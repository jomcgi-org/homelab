#!/usr/bin/env bash
# Run from the repo root on an isolated Linux amd64 runner.
set -euo pipefail
umask 077
tmp=$(mktemp -d)
etcd_pid=
api_pid=
cleanup() {
	for pid in "$api_pid" "$etcd_pid"; do
		if [[ -n $pid ]]; then kill "$pid" 2>/dev/null || true; fi
	done
	wait || true
	rm -rf "$tmp"
}
trap cleanup EXIT
wget -qO "$tmp/tools.tgz" https://github.com/kubernetes-sigs/controller-tools/releases/download/envtest-v1.34.0/envtest-v1.34.0-linux-amd64.tar.gz
echo "9c45e40aa56971b105e596ebb3e84af6742e8709cc0523733baf8d9bb725e69c  $tmp/tools.tgz" | sha256sum -c -
tar xzf "$tmp/tools.tgz" -C "$tmp"
export PATH="$tmp/controller-tools/envtest:$PATH" KUBECONFIG="$tmp/config"
openssl genrsa -out "$tmp/key" 2048
token=$(openssl rand -hex 32)
echo "$token,tester,1,system:masters" >"$tmp/users"
etcd --data-dir="$tmp/etcd" >"$tmp/etcd.log" 2>&1 &
etcd_pid=$!
kube-apiserver --etcd-servers=http://127.0.0.1:2379 --bind-address=127.0.0.1 --advertise-address=127.0.0.1 --secure-port=16443 --cert-dir="$tmp" --service-cluster-ip-range=10.0.0.0/24 --service-account-issuer=https://kubernetes.default.svc --service-account-key-file="$tmp/key" --service-account-signing-key-file="$tmp/key" --token-auth-file="$tmp/users" >"$tmp/api.log" 2>&1 &
api_pid=$!
kubectl config set-cluster test --server=https://127.0.0.1:16443 --insecure-skip-tls-verify=true
kubectl config set-credentials test --token="$token" >/dev/null
kubectl config set-context test --cluster=test --user=test
kubectl config use-context test
for _ in {1..60}; do
	kubectl get --raw=/readyz >/dev/null 2>&1 && break
	kill -0 "$api_pid" "$etcd_pid"
	sleep 1
done
kubectl create namespace ships
kubectl create namespace argocd
tar xOzf projects/platform/onepassword/charts/connect-2.0.5.tgz connect/crds/onepassworditem-crd.yaml | kubectl create -f -
kubectl wait --for=condition=Established crd/onepassworditems.onepassword.com
kubectl apply -f projects/platform-factory/onepassword/admission.yaml
# Admission registration propagates asynchronously.
for _ in {1..30}; do
	if projects/platform-factory/onepassword/test-admission.sh; then exit 0; fi
	sleep 1
done
exit 1
