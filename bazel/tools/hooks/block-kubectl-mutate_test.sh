#!/usr/bin/env bash
# Unit tests for block-kubectl-mutate.sh PreToolUse hook.

set -euo pipefail

HOOK_REL="bazel/tools/hooks/block-kubectl-mutate.sh"
HOOK=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/${HOOK_REL}" \
	"${TEST_SRCDIR:-}/_main/${HOOK_REL}" \
	"${BASH_SOURCE[0]%/*}/block-kubectl-mutate.sh"; do
	if [[ -f "$candidate" ]]; then
		HOOK="$candidate"
		break
	fi
done
if [[ -z "$HOOK" ]]; then
	echo "ERROR: cannot locate block-kubectl-mutate.sh in runfiles" >&2
	exit 1
fi

fails=0

RATCHET=""
for candidate in \
	"${RUNFILES_DIR:-}/_main/bazel/tools/ci/source_ratchet.py" \
	"${TEST_SRCDIR:-}/_main/bazel/tools/ci/source_ratchet.py" \
	"${BASH_SOURCE[0]%/*}/../ci/source_ratchet.py"; do
	if [[ -f "$candidate" ]]; then
		RATCHET="$candidate"
		break
	fi
done

run() {
	python3 -c 'import json,sys; print(json.dumps({"tool_input": {"command": sys.argv[1]}}))' "$1" |
		python3 "$RATCHET" --hook 2>/dev/null
}

expect() {
	local want="$1" cmd="$2" got=0
	run "$cmd" || got=$?
	if [[ "$got" != "$want" ]]; then
		echo "FAIL: want exit $want, got $got: $cmd" >&2
		fails=$((fails + 1))
	fi
}

# Blocked writes.
expect 2 "kubectl apply -f x.yaml"
expect 2 "kubectl -n monolith delete pod x"
expect 2 "kubectl --context homelab-hub patch application monolith -n argocd --type merge -p '{}'"
expect 2 "kubectl scale deploy/x --replicas=0"
expect 2 "kubectl rollout restart deploy/x -n a"
expect 2 "kubectl create secret generic x --from-literal=a=b"
expect 2 "kubectl label node n a=b"
expect 2 "cd /tmp && kubectl edit cm x"
expect 2 "kubectl drain node-1"

expect 2 "git status && kubectl delete pod x"
expect 2 "gh pr view 1; kubectl delete pod x"
expect 2 "kubectl apply --dry-run=server -f x && kubectl apply -f x"
expect 2 "bash -c 'kubectl delete pod x'"
expect 2 "kubectl --insecure-skip-tls-verify delete pod x"
expect 2 "kubectl -nargocd delete pod x"
expect 2 "kubectl create -n foo secret generic x"
expect 2 "kubectl create sa x"
expect 2 "kubectl rollout undo deploy/x"
expect 2 "helm uninstall x -n a"
expect 2 "helm upgrade --install x ./chart"

# Allowed.
expect 0 "cat x.md  # mentions kubectl delete"
expect 0 "python3 - <<'PY'
print('kubectl delete pod x')
PY"
expect 0 "rg 'kubectl apply' docs/"
expect 0 "kubectl get deploy scale-test"
expect 0 "kubectl get cm patch-config -n x"
expect 0 "kubectl logs deploy/x | grep delete"
expect 0 "kubectl exec -it pod/x -- sh"
expect 0 "kubectl port-forward svc/x 8080:80"
expect 0 "kubectl rollout history deploy/x"
expect 0 "kubectl --context=homelab-hub top pods -A"
expect 0 "helm template x ./chart -f values.yaml"
expect 0 "kubectl get pods -A"
expect 0 "kubectl -n argocd get application monolith -o jsonpath='{.spec}'"
expect 0 "kubectl describe pod x"
expect 0 "kubectl logs deploy/x --tail=50"
expect 0 "kubectl rollout status deploy/x"
expect 0 "kubectl apply --dry-run=server -f x.yaml"
expect 0 "kubectl create token embervm -n embervm"
expect 0 "kubectl auth can-i delete pods"
expect 0 "git commit -m 'docs: never kubectl apply'"
expect 0 "ls -la"

if [[ "$fails" -ne 0 ]]; then
	echo "$fails case(s) failed" >&2
	exit 1
fi
echo "PASS"
