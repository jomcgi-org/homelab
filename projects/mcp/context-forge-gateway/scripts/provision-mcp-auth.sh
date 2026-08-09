#!/usr/bin/env bash
# Provision the runtime half of root-endpoint MCP auth.
#
# The chart gives Envoy the RFC 9728 discovery document. This gives Context
# Forge the three database objects that make the resulting token mean something:
#
#   1. a team, which is what tool entitlements hang off;
#   2. an sso_providers row with trusted_for_api_auth=true, so a bearer token
#      from authentik is accepted at the ROOT /mcp endpoint rather than only on
#      a virtual server;
#   3. team_mapping, so an authentik group name becomes membership of that team
#      at provisioning time. Membership is then read from the database, never
#      from token claims (see verify_credentials: token_use="session" resolves
#      via resolve_session_teams, not normalize_token_teams).
#
# None of this is in Git otherwise: Context Forge keeps it in Postgres, and that
# database was rebuilt from scratch once already. Re-running this restores it.
#
# Idempotent: every step is create-or-update, so it is safe to run repeatedly
# and safe to run after a database rebuild.
#
# Usage: CLIENT_ID=<authentik client id> ./provision-mcp-auth.sh
set -euo pipefail

NS=mcp
TEAM_NAME=${TEAM_NAME:-homelab-admin}
# The authentik group whose members should land in TEAM_NAME. Created by the
# authentik blueprint, not by this script.
AUTHENTIK_GROUP=${AUTHENTIK_GROUP:-homelab-admin}
ISSUER=${ISSUER:-https://auth.jomcgi.dev/application/o/mcp-friends/}
AUTHENTIK_BASE=${AUTHENTIK_BASE:-https://auth.jomcgi.dev}
# authentik mints `aud` = client_id and ignores the RFC 8707 `resource`
# parameter, so the audience Context Forge must expect IS the client id.
CLIENT_ID=${CLIENT_ID:?set CLIENT_ID to the authentik OAuth2 provider client id}
ADMIN_EMAIL=${ADMIN_EMAIL:-joe@jomcgi.dev}

die() {
	echo "ERROR: $*" >&2
	exit 1
}

POD=$(kubectl get pod -n "$NS" -l app=context-forge-gateway-mcp-stack-mcpgateway \
	--field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
[ -n "$POD" ] || die "no running gateway pod found"
echo "gateway pod: $POD"

run() { kubectl exec -n "$NS" "$POD" -- sh -c "$1"; }

# Assert the PROCESS env, not the manifest. Config set only in the ConfigMap
# does not roll this deployment (the mcp-stack pod template has no annotations
# block, so there is no checksum to change), and `kubectl rollout status` still
# reports success because the deployment really is at its desired state.
live_sso=$(run 'printf %s "${SSO_ENABLED:-unset}"')
[ "$live_sso" = "true" ] || die "pod has SSO_ENABLED=$live_sso, so /auth/sso is not mounted.
Set it under mcpContextForge.extraEnv (NOT config) so the pod rolls, or restart:
  kubectl rollout restart deploy/context-forge-gateway-mcp-stack-mcpgateway -n $NS"

# Short-lived admin JWT, minted inside the pod so the signing key never leaves it.
run "cd /app && python3 -m mcpgateway.utils.create_jwt_token -u '$ADMIN_EMAIL' --admin -e 10 2>/dev/null | tail -1 > /tmp/.pv_tok"
API_BODY=$(mktemp)
trap 'rm -f "$API_BODY"; kubectl exec -n "$NS" "$POD" -- rm -f /tmp/.pv_tok /tmp/.pv_body /tmp/.pv_out >/dev/null 2>&1 || true' EXIT

# api sets $API_STATUS and writes the response body to $API_BODY.
#
# It deliberately does NOT print the body on stdout: callers would then wrap it
# in $(...), which runs the function in a subshell and silently discards
# API_STATUS. That is exactly how an earlier version of this script reported
# "HTTP :" against a perfectly healthy 200. Status and body are also fetched in
# two steps rather than with a `-w '\n%{http_code}'` sentinel, because splitting
# on a trailing newline is fragile through kubectl exec.
API_STATUS=""
api() { # api METHOD PATH [JSON]
	local method=$1 path=$2 body=${3:-}
	if [ -n "$body" ]; then
		local b64
		b64=$(printf '%s' "$body" | base64 | tr -d '\n')
		API_STATUS=$(run "echo '$b64' | base64 -d > /tmp/.pv_body && curl -sS -X $method \
      -H \"Authorization: Bearer \$(cat /tmp/.pv_tok)\" -H 'Content-Type: application/json' \
      --data @/tmp/.pv_body -o /tmp/.pv_out -w '%{http_code}' http://localhost:4444$path")
	else
		API_STATUS=$(run "curl -sS -X $method -H \"Authorization: Bearer \$(cat /tmp/.pv_tok)\" \
      -o /tmp/.pv_out -w '%{http_code}' http://localhost:4444$path")
	fi
	run "cat /tmp/.pv_out" >"$API_BODY"
}

api_ok() { # api_ok METHOD PATH [JSON] -- dies unless 2xx
	api "$@"
	case "$API_STATUS" in
	2*) ;;
	*) die "$1 $2 returned HTTP ${API_STATUS:-<none>}: $(head -c 300 "$API_BODY")" ;;
	esac
}

# ── 1. Team ────────────────────────────────────────────────────────────────
api_ok GET /teams/
TEAM_ID=$(python3 -c "
import json,sys
teams=json.load(sys.stdin)
teams=teams.get('teams', teams) if isinstance(teams, dict) else teams
print(next((t['id'] for t in teams if t.get('name')=='$TEAM_NAME'), ''))
" <"$API_BODY")

if [ -z "$TEAM_ID" ]; then
	api_ok POST /teams/ \
		"{\"name\":\"$TEAM_NAME\",\"description\":\"Full monolith catalogue. Mapped from the authentik group of the same name.\",\"visibility\":\"private\"}"
	TEAM_ID=$(python3 -c "import json,sys; print(json.load(sys.stdin)['id'])" <"$API_BODY")
	echo "created team $TEAM_NAME -> $TEAM_ID"
else
	echo "team $TEAM_NAME already exists -> $TEAM_ID"
fi
[ -n "$TEAM_ID" ] || die "team id came back empty"

# ── 2. Trusted external IdP ────────────────────────────────────────────────
# client_secret is required by the request schema but unused on the API-auth
# path: the provider is a PUBLIC client (PKCE, no secret), and this row exists
# so Context Forge can verify signatures via jwks_uri, not to start a flow.
read -r -d '' PROVIDER <<JSON || true
{
  "id": "authentik",
  "name": "authentik",
  "display_name": "authentik",
  "provider_type": "oidc",
  "client_id": "$CLIENT_ID",
  "client_secret": "",
  "authorization_url": "$AUTHENTIK_BASE/application/o/authorize/",
  "token_url": "$AUTHENTIK_BASE/application/o/token/",
  "userinfo_url": "$AUTHENTIK_BASE/application/o/userinfo/",
  "issuer": "$ISSUER",
  "jwks_uri": "${ISSUER}jwks/",
  "scope": "openid profile email",
  "auto_create_users": true,
  "trusted_for_api_auth": true,
  "api_audience": "$CLIENT_ID",
  "team_mapping": {"$AUTHENTIK_GROUP": "$TEAM_ID"}
}
JSON

api GET /auth/sso/admin/providers/authentik
if [ "$API_STATUS" = "200" ]; then
	api_ok PUT /auth/sso/admin/providers/authentik "$PROVIDER"
	echo "updated sso provider 'authentik'"
else
	api_ok POST /auth/sso/admin/providers "$PROVIDER"
	echo "created sso provider 'authentik'"
fi

# ── 3. Verify from the database, not from the API's own say-so ─────────────
rows=$(kubectl exec -n "$NS" context-forge-pg-1 -- psql -U postgres -d mcpgateway -t -A \
	-c "select count(*) from sso_providers where issuer='$ISSUER' and trusted_for_api_auth;" 2>/dev/null |
	tr -dc '0-9')
[ "$rows" = "1" ] || die "expected exactly 1 trusted sso_providers row for $ISSUER, found ${rows:-0}"

echo
echo "── resulting sso_providers row ──"
kubectl exec -n "$NS" context-forge-pg-1 -- psql -U postgres -d mcpgateway -x \
	-c "select name, issuer, api_audience, trusted_for_api_auth, team_mapping from sso_providers;" 2>/dev/null |
	grep -v '^Defaulted'
