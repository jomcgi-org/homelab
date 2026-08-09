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
# Usage: ./provision-mcp-auth.sh
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

POD=$(kubectl get pod -n "$NS" -l app=context-forge-gateway-mcp-stack-mcpgateway \
  -o jsonpath='{.items[0].metadata.name}')
[ -n "$POD" ] || { echo "no gateway pod found" >&2; exit 1; }
echo "gateway pod: $POD"

# Short-lived admin JWT, minted inside the pod so the signing key never leaves it.
run() { kubectl exec -n "$NS" "$POD" -- sh -c "$1"; }
run "cd /app && python3 -m mcpgateway.utils.create_jwt_token -u '$ADMIN_EMAIL' --admin -e 10 2>/dev/null | tail -1 > /tmp/.pv_tok"
trap 'kubectl exec -n "$NS" "$POD" -- rm -f /tmp/.pv_tok /tmp/.pv_body >/dev/null 2>&1 || true' EXIT

api() { # api METHOD PATH [JSON]
  local method=$1 path=$2 body=${3:-}
  if [ -n "$body" ]; then
    local b64
    b64=$(printf '%s' "$body" | base64 | tr -d '\n')
    run "echo '$b64' | base64 -d > /tmp/.pv_body && curl -sS -X $method \
      -H \"Authorization: Bearer \$(cat /tmp/.pv_tok)\" -H 'Content-Type: application/json' \
      --data @/tmp/.pv_body http://localhost:4444$path"
  else
    run "curl -sS -X $method -H \"Authorization: Bearer \$(cat /tmp/.pv_tok)\" http://localhost:4444$path"
  fi
}

# ── 1. Team ────────────────────────────────────────────────────────────────
TEAM_ID=$(api GET /teams/ | python3 -c "
import json,sys
teams=json.load(sys.stdin)
teams=teams.get('teams', teams) if isinstance(teams, dict) else teams
print(next((t['id'] for t in teams if t.get('name')=='$TEAM_NAME'), ''))
")
if [ -z "$TEAM_ID" ]; then
  TEAM_ID=$(api POST /teams/ "{\"name\":\"$TEAM_NAME\",\"description\":\"Full monolith catalogue. Mapped from the authentik group of the same name.\",\"visibility\":\"private\"}" \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
  echo "created team $TEAM_NAME -> $TEAM_ID"
else
  echo "team $TEAM_NAME already exists -> $TEAM_ID"
fi

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

if api GET /auth/sso/admin/providers/authentik | grep -q '"id"'; then
  api PUT /auth/sso/admin/providers/authentik "$PROVIDER" >/dev/null
  echo "updated sso provider 'authentik'"
else
  api POST /auth/sso/admin/providers "$PROVIDER" >/dev/null
  echo "created sso provider 'authentik'"
fi

echo
echo "── resulting sso_providers row ──"
kubectl exec -n "$NS" context-forge-pg-1 -- psql -U postgres -d mcpgateway -x \
  -c "select name, issuer, api_audience, trusted_for_api_auth, team_mapping from sso_providers;" 2>/dev/null \
  | grep -v '^Defaulted' || true
