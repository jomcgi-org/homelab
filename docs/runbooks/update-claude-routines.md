---
name: update-claude-routines
invoke: explicit
summary: Sync claude.ai routines from version-controlled YAML
---

> **Runbook (explicit-only).** Open only when Joe asks for this procedure, or a
> claude.ai routine prompt names this file. Do not auto-load from skill matching.

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
   user supplied a routine slug as an arg (e.g.
   `/update-claude-routines backlog-audit`), filter to that one file. If no
   YAML matches the slug, print "No YAML in projects/monolith/claude_routines/
   has slug '<arg>'. Available: <list of slugs>." and exit (no partial-apply).
   Otherwise reconcile all.

3. **Read each YAML.** Parse in-context — just Read the file and reason about
   its structure; no Python or external script needed. Note that `prompt: |`
   (YAML literal block) PRESERVES the trailing newline of the prompt content
   and the API stores it verbatim — relevant if anyone ever adds a diff-based
   equality check.

4. **Validate against schema.json.** Read
   `projects/monolith/claude_routines/schema.json` and confirm each YAML
   conforms. Validation is **by inspection** — there's no JSON Schema library
   to invoke in-context; compare YAML fields against the schema's `required`,
   `type`, `enum`, and `pattern` constraints and surface mismatches. On
   failure, print the JSON path of the bad field and exit BEFORE any
   RemoteTrigger calls. Idempotency: nothing partially applied.

5. **Resolve names → IDs.** For each YAML:
   - Replace `environment: <name>` with `environment_id: <env_id>` using the
     table above. Fail with a clear message if the name is unknown.
   - Replace each `mcp_connectors[i]` short name with the full
     `{connector_uuid, name, url}` object using the table above. Fail with a
     link to https://claude.ai/customize/connectors if any name is unknown.

6. **Build the API body.** Generate a fresh lowercase v4 UUID for
   `events[0].data.uuid` — yes, even on UPDATE. The API replaces the prior
   UUID without observable churn (verified empirically); preserving the
   existing UUID via `RemoteTrigger get` is optional; nothing depends on
   the UUID surviving.
   The prompt content from the YAML goes in `events[0].data.message.content`
   verbatim.

   Always send the **full body** — never a partial update. See step 7c on the
   always-update rule.

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

   The `allowed_tools: []` in the body is silently replaced by the API with
   a 17-item preset list at write time (see Notes); send `[]` anyway — don't
   try to pre-populate.

7. **Reconcile.** For each YAML:

   a. **Get the current state.** Call `RemoteTrigger` action `list`. The
   response shape is `{data: [<trigger>...], has_more: bool}`. If
   `has_more` is true the response is paginated; the API does not yet
   expose a documented cursor, so treat any `has_more: true` as a
   "should-not-happen" error — print loudly and stop without mutating.
   (A user with >~50 routines is the only realistic trigger; revisit
   when it actually happens.)

   b. **Match by name.** Each item in `data` is a bare trigger object — NOT
   wrapped in `{trigger: ...}`. That wrapper appears only on `get`,
   `create`, and `update` responses. The YAML's `name` field is compared
   verbatim against each `data[i].name` — exact string match, spaces and
   parentheses preserved, no case folding, no slugification.

   c. **Decide create vs update — and always update on match.** If no
   trigger matches: call `action: create` with the full body from step
   6 (no `trigger_id`). If a trigger matches: call `action: update` with
   `trigger_id` set to that trigger's `id`, passing the full body.

   **Do not compute a diff to skip the update.** Diffing is brittle for
   at least four reasons:
   - the API overrides `allowed_tools` (your `[]` becomes a 17-item list),
   - the server jitters `next_run_at` and `updated_at`,
   - YAML `prompt: |` keeps a trailing newline that the API stores verbatim,
   - `creator.display_name` and `permitted_tools` are returned but are
     not sendable fields.

   Always update when name matches. The API is idempotent for repeated
   full-body writes with the same content. Cost: one API call per
   routine per run.

   d. **Read the response.** Both `create` and `update` wrap the result as
   `{trigger: {<full trigger object>}}`. Pull `trigger.id` for
   reporting; pull `trigger.next_run_at` to confirm the schedule was
   accepted.

8. **Warn about orphans.** Any trigger in `list.data` whose `name` does
   NOT appear in any YAML is an orphan. Print its name and ID, but DO NOT
   delete it — claude.ai routines can only be removed via the web UI
   (https://claude.ai/code/routines). Direct the user there if they want to
   prune.

9. **Print a summary — always, even on zero-change runs.** Format:

   ```
   Reconciliation summary:
     Created:    1  (new-routine-name)
     Updated:    1  (Backlog Audit (every 6h))
     Unchanged:  0
     Orphans:    0
   ```

   For created/updated routines include the trigger ID and the claude.ai
   URL: `https://claude.ai/code/routines/<trigger_id>`. With the
   always-update rule from step 7c, `Unchanged` will usually be 0 — that's
   expected, not a bug. The column exists for a future diff-based skip.

## Errors and edge cases

- **YAML schema failure** → print the JSON path of the bad field, exit
  before any API calls. Idempotency: nothing partially applied.
- **Unknown `environment` name** → "Environment 'X' is not in the known
  list ({Default, Unsafe}). Add it to this skill's resolution table if you
  have registered a new environment in claude.ai."
- **Unknown `mcp_connector` name** → "Connector 'X' is not connected on
  this account. Connect it at https://claude.ai/customize/connectors and
  update this skill's resolution table."
- **`run_once_at` in the past** → "run_once_at is in the past
  (<timestamp>). Update the YAML or remove the field."
- **RemoteTrigger 4xx** → print the response body verbatim and exit.
  Common causes: invalid cron, expired claude.ai session, malformed `name`
  on `mcp_connections` (must match `[A-Za-z0-9_-]+`; no dots or spaces).
- **`list` returns `has_more: true`** → pagination is not yet supported in
  this skill. Print a warning ("paginated routine list not supported;
  reconcile may be incomplete") and stop without mutating.
- **API silently replaces `allowed_tools: []`** → expected; do not treat
  the override in the response as drift. The textual constraints in the
  prompt are what actually limit tool use.
- **`permitted_tools: []` on `mcp_connections` in responses** → not a
  sendable field. Omit on send; the API will populate the field with `[]`
  unprompted.
- **Slug arg doesn't match any YAML** → see step 2's exit behavior.

## Notes

- The reconciler is **always read-then-write**, never delete. Source-of-truth
  drift (YAML removed from repo, routine still in claude.ai) surfaces as
  orphans and requires manual cleanup.
- Top-of-hour cron expressions get a few minutes of server-side jitter
  (Anthropic's anti-thundering-herd). Don't treat this as drift.
- Prefer pinned model snapshots (`claude-haiku-4-5-20251001`) over family
  aliases (`claude-haiku-4-5`) for autonomous routines — the alias auto-tracks
  the latest snapshot, which can change behavior under the user.
- **`allowed_tools` override (verified)** — when you send `"allowed_tools": []`
  the API stores this exact list on the routine:
  ```
  preset:default, Task, Bash, Glob, Grep, Read, Edit, MultiEdit, Write,
  NotebookEdit, WebFetch, TodoWrite, WebSearch, BashOutput, KillBash,
  Skill, Tmux, Monitor, SendUserFile, REPL
  ```
  That is the actual tool surface the routine has at runtime. If you need a
  narrower surface, enforce it via the prompt — there's no server-side way.
- **Response envelope asymmetry** — `list` returns trigger objects bare
  inside `data: [...]`; `get`, `create`, and `update` wrap their single
  trigger as `{trigger: {...}}`. Don't mix the two when post-processing.
- **`creator.display_name`** appears on `get`/`create`/`update` responses
  but is omitted from `list` items. Cosmetic only; don't depend on it for
  matching.
- **Routines that have already fired** carry `last_fired_at` plus an
  advanced `next_run_at`. This is _runtime state_, not drift from the YAML.
- The `data[i]` items on a `list` response include the full `job_config`
  and `mcp_connections` — there is no follow-up `get` needed for matching
  or to read fields. Save the API call.

