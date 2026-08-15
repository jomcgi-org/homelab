#!/usr/bin/env bash
set -euo pipefail

# Call the ArgoCD API directly, without the UI.
#
# Handles the full dance: port-forward argocd-server, read the admin password
# from argocd-initial-admin-secret, exchange it for a session JWT, then call
# the requested API path with that bearer token. The port-forward is cleaned
# up on exit.
#
# Usage: argocd-api.sh <api-path> [curl-args...]
# Example: argocd-api.sh /api/v1/applications/monolith/managed-resources
#
# Uses `kubectl --context local-homelab` -- that context is the one that
# reaches this cluster. Override with ARGOCD_KUBE_CONTEXT if needed.

KUBE_CONTEXT="${ARGOCD_KUBE_CONTEXT:-local-homelab}"
CANDIDATE_PORTS=(8080 8081 8090 18080)
READY_TIMEOUT_SECS=15

if [ $# -lt 1 ]; then
	echo "Usage: $0 <api-path> [curl-args...]" >&2
	echo "Example: $0 /api/v1/applications/monolith/managed-resources" >&2
	exit 1
fi

api_path="$1"
shift

case "$api_path" in
/*) ;;
*)
	echo "ERROR: api-path must start with /, got: $api_path" >&2
	exit 1
	;;
esac

for bin in kubectl curl jq nc base64; do
	if ! command -v "$bin" &>/dev/null; then
		echo "ERROR: required binary not found in PATH: $bin" >&2
		exit 1
	fi
done

is_port_open() {
	nc -z -w1 127.0.0.1 "$1" &>/dev/null
}

local_port=""
for candidate in "${CANDIDATE_PORTS[@]}"; do
	if ! is_port_open "$candidate"; then
		local_port="$candidate"
		break
	fi
done

if [ -z "$local_port" ]; then
	echo "ERROR: no free local port found among: ${CANDIDATE_PORTS[*]}" >&2
	exit 1
fi

if ! kubectl --context "$KUBE_CONTEXT" -n argocd get svc argocd-server &>/dev/null; then
	echo "ERROR: cannot reach svc/argocd-server via context '$KUBE_CONTEXT'" >&2
	echo "  check the context exists: kubectl config get-contexts" >&2
	exit 1
fi

pf_log="$(mktemp)"
cleanup() {
	if [ -n "${pf_pid:-}" ]; then
		kill "$pf_pid" &>/dev/null || true
		wait "$pf_pid" 2>/dev/null || true
	fi
	rm -f "$pf_log"
}
trap cleanup EXIT

# argocd-server here runs behind Envoy at a subpath, and the API moves with it:
# with server.rootpath=/app/argocd the router serves /app/argocd/api/v1/... and
# a request to /api/v1/... is a plain 404. It also runs server.insecure=true, so
# the container speaks HTTP on 8080 and an https:// request to the forwarded
# port never completes a handshake. Both were wrong here, and the combination
# failed as an EMPTY response rather than an error, so this script reported
# nothing at all and read like "the app has no diff" (which is how five
# genuinely-OutOfSync apps looked clean). Read both from the cluster rather than
# hardcoding, so a change to either keeps working.
server_params="$(kubectl --context "$KUBE_CONTEXT" -n argocd get cm argocd-cmd-params-cm -o json 2>/dev/null)"
api_base="$(printf '%s' "$server_params" | jq -r '.data["server.rootpath"] // ""')"
if [ "$(printf '%s' "$server_params" | jq -r '.data["server.insecure"] // "false"')" = "true" ]; then
	scheme="http"
	svc_port=80
else
	scheme="https"
	svc_port=443
fi

kubectl --context "$KUBE_CONTEXT" -n argocd port-forward svc/argocd-server "${local_port}:${svc_port}" \
	>"$pf_log" 2>&1 &
pf_pid=$!

ready=0
elapsed=0
while [ "$elapsed" -lt "$READY_TIMEOUT_SECS" ]; do
	if ! kill -0 "$pf_pid" &>/dev/null; then
		echo "ERROR: kubectl port-forward exited early" >&2
		cat "$pf_log" >&2
		exit 1
	fi
	if is_port_open "$local_port"; then
		ready=1
		break
	fi
	sleep 1
	elapsed=$((elapsed + 1))
done

if [ "$ready" -ne 1 ]; then
	echo "ERROR: port-forward to argocd-server did not become ready on 127.0.0.1:${local_port} within ${READY_TIMEOUT_SECS}s" >&2
	cat "$pf_log" >&2
	exit 1
fi

admin_password_b64="$(kubectl --context "$KUBE_CONTEXT" -n argocd get secret argocd-initial-admin-secret \
	-o jsonpath='{.data.password}' 2>/dev/null)"

if [ -z "$admin_password_b64" ]; then
	echo "ERROR: argocd-initial-admin-secret not found or empty" >&2
	echo "  (the admin password may have been rotated or the secret deleted after initial setup)" >&2
	exit 1
fi

admin_password="$(printf '%s' "$admin_password_b64" | base64 -d)"

session_body="$(jq -n --arg u admin --arg p "$admin_password" '{username: $u, password: $p}')"

session_response="$(curl -sk -H 'Content-Type: application/json' -X POST \
	-d "$session_body" \
	"${scheme}://127.0.0.1:${local_port}${api_base}/api/v1/session")"

token="$(printf '%s' "$session_response" | jq -r '.token // empty')"

if [ -z "$token" ]; then
	echo "ERROR: failed to obtain ArgoCD session token" >&2
	echo "--- response ---" >&2
	printf '%s\n' "$session_response" >&2
	exit 1
fi

curl -sk -H "Authorization: Bearer ${token}" "${scheme}://127.0.0.1:${local_port}${api_base}${api_path}" "$@"
