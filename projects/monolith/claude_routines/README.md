# Claude.ai Routines (source of truth)

Version-controlled definitions of the user's claude.ai scheduled-agent
**routines** (a.k.a. Claude Code Routines / remote triggers). Each
`*.yaml` file in this directory is one routine. claude.ai is the _runtime_
state; this directory is the _intent_ state. Drift is reconciled by running
the update-claude-routines runbook.

## Files

| Path                    | Purpose                                                                                  |
| ----------------------- | ---------------------------------------------------------------------------------------- |
| `schema.json`           | JSON Schema for routine YAMLs. IDEs (e.g. VSCode + YAML extension) can validate on save. |
| `<slug>.yaml`           | One routine per file. The `name` field is the lookup key when reconciling.               |
| `README.md` (this file) | Pointer to the schema + skill.                                                           |

The reconciliation logic lives in the
[`update-claude-routines` runbook](../../../docs/runbooks/update-claude-routines.md) —
it explains the schema, the name → ID resolution rules for `environment` and
`mcp_connectors`, the workflow, and the error modes.

## Adding a routine

1. Copy an existing YAML (e.g. `backlog-audit.yaml`) and edit `name`, `schedule`,
   `prompt`, and any other relevant fields.
2. Validate against `schema.json` — your IDE will do this automatically if the
   first line of the YAML is the `yaml-language-server` schema directive shown
   in the existing files.
3. PR + merge.
4. In a Claude Code session, run `/update-claude-routines` (or
   `/update-claude-routines <slug>` to reconcile just that one). The skill
   creates or updates the claude.ai routine to match.

## Editing a routine

Same as adding — change the YAML, PR, merge, run `/update-claude-routines`.
The skill matches existing claude.ai routines by `name`, so renaming a routine
in YAML _creates a new one in claude.ai_; the old one becomes an orphan that
must be deleted manually via https://claude.ai/code/routines.

## Removing a routine

Delete the YAML, PR, merge. The skill will report the corresponding claude.ai
routine as an orphan but **will not delete it** — deletion is only possible via
the web UI at https://claude.ai/code/routines. This is intentional: routines
carry execution history (`last_run_at`, `ended_reason`) that a silent delete
would lose.

## No UUIDs in this directory

By design — UUIDs (env_ids, connector UUIDs) are account-specific runtime
identifiers, not configuration. Routine YAMLs reference `environment` and
`mcp_connectors` by short name; the skill resolves to IDs at sync time using
the current tables in its runbook. If the user adds a new environment or
connects a new connector, update the runbook's resolution tables in the same PR.
