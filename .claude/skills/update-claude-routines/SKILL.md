---
name: update-claude-routines
description: Reconcile claude.ai scheduled-agent routines from version-controlled YAML in projects/monolith/claude_routines/. Resolves environment and mcp_connectors names against live claude.ai state at sync time. Invoke via /update-claude-routines, or use when the user has edited a routine YAML, asks to sync scheduled remote agents, or asks how the homelab's routine schema works.
---

# /update-claude-routines

This skill keeps the user's claude.ai scheduled-agent routines in sync with the
YAML definitions checked into `projects/monolith/claude_routines/`. The repo is
the source of truth; running this skill brings live claude.ai state into
alignment.

## When to invoke

- User types `/update-claude-routines` (optionally with a routine name as arg
  to reconcile just that one routine)
- User edits a file under `projects/monolith/claude_routines/`
- User asks to sync, push, deploy, or reconcile scheduled remote agents
- User asks "what's the schema for these YAMLs?" or "how do routine YAMLs work?"

## What the YAMLs look like

Each routine is one file at `projects/monolith/claude_routines/<slug>.yaml`.
Full JSON Schema lives at `projects/monolith/claude_routines/schema.json` —
prefer to read that for the authoritative spec. Summary:

```yaml
name: Backlog Audit (every 6h) # required; human-readable, used as the lookup key
schedule: # required; pick one of cron or run_once_at
  cron: "0 */6 * * *" # 5-field cron in UTC; minimum interval 1h
  # run_once_at: "2026-05-23T15:00:00Z"  # RFC3339 UTC, future-dated
enabled: true # default true
model: claude-haiku-4-5-20251001 # pinned snapshot OR family alias (claude-haiku-4-5)
environment: Default # NAME, not env_id (see Resolution rules)
sources: # repos the agent's git workspace clones; optional
  - https://github.com/jomcgi/homelab
allowed_tools: [] # NOTE: API silently overrides [] with preset:default
mcp_connectors: # short NAMES, not UUIDs (see Resolution rules)
  - homelab
prompt: | # required; the user-message content the routine fires with
  ...
```

## Resolution rules — names → IDs at sync time

The repo is deliberately UUID-free. Two fields are resolved from short names to
account-specific IDs at sync time:

### `environment` → `environment_id`

| Name      | env_id                         | Notes                   |
| --------- | ------------------------------ | ----------------------- |
| `Default` | `env_011CUPNj26sqQQ9rLLJ6fZ7v` | Standard sandbox        |
| `Unsafe`  | `env_011CUy9XmpTNSeNNCD1FDLWo` | Elevated; use sparingly |

If the user adds a new environment, update this table.

### `mcp_connectors[i]` (string) → full `{connector_uuid, name, url}` object

Current connectors connected on the user's claude.ai account:

| Short name        | connector_uuid                         | URL                                         |
| ----------------- | -------------------------------------- | ------------------------------------------- |
| `homelab`         | `c1cf153a-5323-43aa-9a3a-c01e08998732` | `https://mcp.jomcgi.dev/mcp`                |
| `Google-Drive`    | `5080f8ce-2d1b-45c9-abeb-b0029012cc5b` | `https://drivemcp.googleapis.com/mcp/v1`    |
| `Google-Calendar` | `b10484c4-1f11-439f-b1fe-c0918d15dbcc` | `https://calendarmcp.googleapis.com/mcp/v1` |
| `Gmail`           | `c45373a8-c26e-436b-9285-5b747e0dcc77` | `https://gmailmcp.googleapis.com/mcp/v1`    |

If the user adds or removes a connector via https://claude.ai/customize/connectors,
update this table. If a YAML references a connector name not in this table,
fail with a clear message linking the user to that URL.

The `name` field passed to the API must match `[a-zA-Z0-9_-]+` (no dots, no
spaces). The short names above already conform.

## Workflow

1. **Load tools.** RemoteTrigger is a deferred tool — call
   `ToolSearch` with `select:RemoteTrigger` to load it.

2. **Find the YAMLs.** Glob `projects/monolith/claude_routines/*.yaml`. If the
   user supplied a routine name as an arg (e.g. `/update-claude-routines backlog-audit`),
   filter to that one file. Otherwise reconcile all.

3. **Read each YAML.** Parse it with the user's YAML library of choice (just
   Read the file and parse in-context — no need for a script).

4. **Validate.** Read `projects/monolith/claude_routines/schema.json` and
   confirm each YAML conforms. On failure, print the JSON Schema error path
   (e.g., `mcp_connectors[0]: must be a string`) and exit before any
   RemoteTrigger calls.

5. **Resolve names.** For each YAML:
   - Replace `environment: <name>` with `environment_id: <env_id>` from the
     table above. Fail with a clear message if the name is unknown.
   - Replace each `mcp_connectors[i]` short name with the full
     `{connector_uuid, name, url}` object from the table above. Fail with a
     link to https://claude.ai/customize/connectors if any name is unknown.

6. **Build the API body.** Each YAML maps to a `RemoteTrigger create`/`update`
   body. Generate a fresh lowercase v4 UUID for `events[0].data.uuid`. The
   prompt content goes in `events[0].data.message.content`.

   The full body shape (matching the /schedule skill):

   ```json
   {
     "name": "...",
     "cron_expression": "..." OR "run_once_at": "...",
     "enabled": true,
     "job_config": {
       "ccr": {
         "environment_id": "...",
         "session_context": {
           "model": "...",
           "sources": [{"git_repository": {"url": "..."}}],
           "allowed_tools": []
         },
         "events": [{"data": {
           "uuid": "<fresh v4>",
           "session_id": "",
           "type": "user",
           "parent_tool_use_id": null,
           "message": {"role": "user", "content": "<prompt>"}
         }}]
       }
     },
     "mcp_connections": [{"connector_uuid": "...", "name": "...", "url": "..."}]
   }
   ```

7. **Reconcile.** For each YAML:
   - Call `RemoteTrigger` action `list` to get all existing routines.
   - Find the routine with matching `name`. If none, call `action: create`
     with the body. If one exists, call `action: update` with `trigger_id`
     set to the existing one's `id` and `body` containing only the changed
     fields (partial update — pass the full body if computing the diff is
     awkward, the API tolerates it).

8. **Warn about orphans.** Any claude.ai routine whose name does NOT appear
   in any YAML is an orphan. Print its name and ID, but DO NOT delete it —
   claude.ai routines can only be removed via the web UI
   (https://claude.ai/code/routines). Direct the user there if they want to
   prune.

9. **Print a summary.** Format like:

   ```
   Reconciliation summary:
     Created:    1  (new-routine-name)
     Updated:    1  (Backlog Audit (every 6h))
     Unchanged:  0
     Orphans:    0
   ```

   For created/updated routines, include the trigger ID and the claude.ai URL:
   `https://claude.ai/code/routines/<trigger_id>`.

## Errors and edge cases

- **YAML schema failure** → print the JSON path of the bad field, exit before
  any API calls. Idempotency: nothing partially-applied.
- **Unknown `environment` name** → "Environment 'X' is not in the known list
  ({Default, Unsafe}). Add it to this skill's resolution table if you've
  registered a new environment in claude.ai."
- **Unknown `mcp_connector` name** → "Connector 'X' is not connected on this
  account. Connect it at https://claude.ai/customize/connectors and update
  this skill's resolution table."
- **`run_once_at` in the past** → "run_once_at is in the past
  (<timestamp>). Update the YAML or remove the field."
- **RemoteTrigger 4xx** → print the response body verbatim and exit. Common
  causes: `name` collision (a routine with the same name was created
  out-of-band), invalid cron, expired claude.ai session.
- **API silently overrides `allowed_tools: []`** → expected behavior. Don't
  treat the override in the response as drift; the textual constraints in
  the routine's prompt are what actually limit tool use.

## Notes

- The reconciler is **always read-then-write**, never delete. Source-of-truth
  drift (YAML removed from repo, routine still in claude.ai) surfaces as
  orphans and requires manual cleanup.
- Top-of-hour cron expressions get a few minutes of server-side jitter
  (Anthropic's anti-thundering-herd). Don't treat this as drift.
- Prefer pinned model snapshots (`claude-haiku-4-5-20251001`) over family
  aliases (`claude-haiku-4-5`) for autonomous routines — the alias auto-tracks
  the latest snapshot, which can change behavior under the user.
