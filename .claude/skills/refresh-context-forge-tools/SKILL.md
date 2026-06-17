---
name: refresh-context-forge-tools
description: Make Context Forge (the mcpgateway in the `mcp` namespace) re-discover a federated gateway's MCP tools after they change. Context Forge caches each gateway's tool catalog in its own Postgres and does NOT auto-refresh, so newly added or edited monolith MCP tools stay invisible to the `homelab` connector / claude.ai routines until you run this. Invoke via /refresh-context-forge-tools, or when the user says new MCP tools "aren't showing up", "weren't picked up by Context Forge", or asks how to make Context Forge see updated tools. Do NOT rollout-restart the monolith or the gateway for this; it won't help.
---

# /refresh-context-forge-tools

Context Forge (IBM `mcp-context-forge`, image `ghcr.io/ibm/mcp-context-forge`)
fronts the monolith's MCP server and federates its tools out to the `homelab`
connector (which is what claude.ai routines and Claude Code Web reach). It
**caches the upstream tool catalog in its own Postgres and does not
re-discover automatically** (`AUTO_REFRESH_SERVERS=false`; it only connects to
the monolith per tool _invocation_, never to re-list). So after you add or edit
an `@mcp.tool` in the monolith (e.g. `projects/monolith/knowledge/mcp.py`),
deploying the monolith is necessary but **not sufficient** for the tool to
appear downstream. You must explicitly tell Context Forge to refresh.

## When to invoke

- User types `/refresh-context-forge-tools` (optionally naming a gateway;
  default is `monolith`).
- New or changed monolith MCP tools "aren't showing up" / "weren't picked up
  by Context Forge" / aren't visible to a claude.ai routine.
- After merging a PR that adds/removes/renames MCP tools and the rollout is live.

## What will NOT fix it (don't suggest these)

- **Rollout-restarting the monolith** — it already serves the new tools; the
  stale catalog lives in Context Forge, not the monolith.
- **Restarting the Context Forge pod** — on boot it loads the catalog from its
  own Postgres rather than re-discovering, so you reload the same stale rows and
  briefly drop every MCP tool for all consumers.
- **Enabling `AUTO_REFRESH_SERVERS=true` in Git** — it's a likely no-op for the
  monolith gateway: auto-refresh runs _inside_ the health-check loop gated on a
  successful health tick, but the loop probes with `streamablehttp_client` while
  the monolith serves the **SSE** transport, so `monolith.last_seen` stays
  frozen and the refresh block never fires. (Confirmed 2026-06-17: last_seen was
  stuck at the registration date for 7.5 days.)

## How it works

The reliable lever is the admin endpoint
`POST /gateways/{gateway_id}/tools/refresh`, which re-lists the gateway's tools
and upserts. It returns a `GatewayRefreshResponse`:
`{toolsAdded, toolsUpdated, toolsRemoved, validationErrors, ...}`. Because
`API_ALLOW_BASIC_AUTH=false` and `AUTH_REQUIRED=true`, you must present a JWT
bearer token; mint one inside the pod with the bundled
`mcpgateway.utils.create_jwt_token` util (it reads `JWT_SECRET_KEY` from the
pod env automatically).

## Coordinates

| Thing           | Value                                                                                                     |
| --------------- | --------------------------------------------------------------------------------------------------------- |
| Namespace       | `mcp`                                                                                                     |
| Deployment      | `context-forge-gateway-mcp-stack-mcpgateway`                                                              |
| App container   | `mcp-context-forge` (NOT container[0], which is `linkerd-proxy`)                                          |
| Port            | `4444`                                                                                                    |
| Admin email     | `joe@jomcgi.dev` (`PLATFORM_ADMIN_EMAIL`)                                                                 |
| CF Postgres pod | `context-forge-gateway-mcp-stack-postgres-*`                                                              |
| CF DB / user    | db `postgresdb`, user `admin` (creds in `context-forge-gateway-mcp-stack-postgres-secret`)                |
| Tables          | `gateways` (id, name, url, enabled, reachable, last_seen, last_refresh_at), `tools` (name, gateway_id FK) |
| Repo config     | `projects/mcp/context-forge-gateway/deploy/values.yaml` -> `mcp-stack.mcpContextForge.config`             |

## Workflow

1. **Find the gateway id.** Default to `monolith` unless the user names another.

   ```sh
   PGPOD=$(kubectl get po -n mcp -l app=context-forge-gateway-mcp-stack-postgres -o name | head -1)
   PGPOD=${PGPOD#pod/}
   kubectl exec -n mcp "$PGPOD" -c postgres -- \
     psql -U admin -d postgresdb -t -c "select id, name from gateways where name='monolith';"
   ```

   If the label selector finds nothing, list all pods in `mcp` and pick the
   postgres one by name.

2. **Run the refresh.** Pipe the script via stdin to avoid nested-quote
   mangling by the local shell. Substitute the gateway id from step 1.

   ```sh
   GW=context-forge-gateway-mcp-stack-mcpgateway
   POD=$(kubectl get po -n mcp -l app=$GW -o name | head -1); POD=${POD#pod/}
   cat > /tmp/cf_refresh.sh <<'EOF'
   TOKEN=$(python3 -m mcpgateway.utils.create_jwt_token -u joe@jomcgi.dev --admin -e 10 2>/dev/null | tail -1)
   export TOKEN
   python3 - <<'PY'
   import os, httpx
   gid="<GATEWAY_ID>"
   r=httpx.post(f"http://localhost:4444/gateways/{gid}/tools/refresh",
                headers={"Authorization":"Bearer "+os.environ["TOKEN"]}, timeout=60)
   print(r.status_code, r.text)
   PY
   EOF
   sed -i '' "s/<GATEWAY_ID>/<the id>/" /tmp/cf_refresh.sh   # or edit the file directly
   kubectl exec -i -n mcp "$POD" -c mcp-context-forge -- sh < /tmp/cf_refresh.sh
   ```

   A healthy result is HTTP `200` with `"success":true` and a non-empty
   `toolsAdded`/`toolsUpdated`. **Check `validationErrors` and watch for tools
   that were silently dropped** — Context Forge rejects any tool whose
   description contains a forbidden pattern (`&&`, `;`, `||`, `$(`, `|`,
   `> `, `< `). If `toolsAdded` is lower than expected, a docstring likely has
   one of those characters; fix it in the monolith, redeploy, and refresh again.

3. **Verify the catalog.** Confirm the expected tools are now present.

   ```sh
   kubectl exec -n mcp "$PGPOD" -c postgres -- psql -U admin -d postgresdb -t -c \
     "select t.name from tools t join gateways g on t.gateway_id=g.id
      where g.name='monolith' order by t.name;"
   ```

## Notes

- This is a runtime admin action on Context Forge's own state, not a Kubernetes
  config change, so it does not violate the GitOps "modify Git instead" rule.
  It is idempotent — safe to run repeatedly.
- If monolith MCP-tool churn ever becomes frequent, the durable upgrade is a
  post-rollout Job/hook that calls this same refresh endpoint automatically,
  rather than running this skill by hand. That is the right place to invest
  before re-attempting `AUTO_REFRESH_SERVERS` (which also needs the health-check
  transport gap fixed first).
- Related: the Context Forge description-sanitization gotcha (forbidden chars in
  tool docstrings) and the monolith MCP surface live alongside the knowledge
  graph work.
