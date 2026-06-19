# Vault-to-Postgres cutover (Obsidian decommission Phase 6)

**Status:** Done (2026-06-18)
**Branch:** chore/retire-obsidian

Completes Phase 6 of the Obsidian decommission (docs/plans/2026-06-13-obsidian-decommission-plan.md):
make every knowledge note read/write path hit Postgres directly and remove the
`/vault` disk dependency entirely, then delete the dead Obsidian plumbing.

## Why now

Obsidian sync was already runtime-disabled on 2026-06-15 (`headlessSync.enabled:
false`, `gitRemote: ""`). The reconciler that ingested disk files into Postgres
was deleted in PR #2680. But the note **write** paths still serialize markdown
to `/vault` (an ephemeral `emptyDir` nothing reads), so:

- `create_note` writes a file and never indexes: post-reconciler it is a no-op
  in prod (latent bug).
- `edit_note` reads the disk file for current frontmatter, writes it back, then
  indexes into Postgres.
- `set/reset_note_visibility`, `delete/undelete_note` write/move files under
  `/vault`.
- The Discord `summarizer` writes channel/user summaries to `/vault` via
  `chat/vault_export.py` (also going nowhere).

Postgres is already authoritative: `knowledge.notes.content` holds the body,
promoted frontmatter keys are columns, `extra` is a JSONB column, and edges are
`note_links` rows. So nothing is lost by going DB-only; the `index_note_from_raw`
helper already takes an in-memory `raw` string with a synthetic `rel_path`
(`create_atom` proves the fileless pattern).

## Tasks

### Task 1 — Core DB-only note read/write refactor

`knowledge/notes.py`:

- Remove `VAULT_ROOT_ENV`, `DEFAULT_VAULT_ROOT`, `get_vault_root`,
  `_write_note_visibility_frontmatter`, `_trash_filename`.
- `resolve_note_body`: return `content` directly (drop the disk fallback);
  callers stop passing `vault_root`.
- `_read_note_snippet` / `_note_to_review_dict` / `list_notes_for_review`: drop
  `vault_root`, read `note.content`.
- `set_note_visibility` / `reset_note_visibility`: update the column only
  (no frontmatter write; `content` carries no frontmatter).
- `delete_note` / `undelete_note`: flip `deleted_at` only (no file move); keep
  `pre_delete_path` semantics as a DB-only breadcrumb or drop.

`knowledge/mcp.py`:

- `create_note`: build raw markdown in memory, resolve a DB-unique `note_id`,
  index via `index_note_from_raw` (mirror `_index_atom`). No disk.
- `edit_note`: reconstruct frontmatter from the DB row (columns + `extra`) and
  edges (`get_note_links`), apply edits, re-serialize, re-index. No disk read or
  write.
- `delete_note` / `get_note`: drop `vault_root`.

`knowledge/router.py` (HTTP `edit_note` ~L279), `knowledge/public_router.py`,
`knowledge/service.py`: drop `vault_root` usage / re-exports.

### Task 2 — Discord summarizer to Postgres

`chat/vault_export.py` + `chat/summarizer.py`: route channel/user summaries
through the fileless index path so they persist in Postgres (today they write to
an ephemeral `emptyDir`). Update `chat/summarizer_test.py`.

### Task 3 — Chart + infra removal (independent)

- `chart/templates/deployment.yaml`: remove `VAULT_ROOT`, `VAULT_API_URL`,
  `VAULT_GIT_REMOTE`/vault-backup git env block, the `/vault` volumeMount, the
  `vault` volume, and the `obsidian` sidecar container.
- `chart/values.yaml`: remove `backend.vaultApiUrl`, `knowledge.headlessSync`,
  `knowledge.vault`, `knowledge.onepassword`, `knowledge.gitRemote`,
  `knowledge.resources.headlessSync`.
- `deploy/values.yaml`: remove `backend.vaultApiUrl`, `knowledge.gitRemote`;
  refresh the stale Obsidian comment.
- `chart/BUILD`: remove the `obsidian_readiness_probe_test` target and the
  `knowledge.headlessSync.image` pin.
- Delete `projects/monolith/obsidian-image/`,
  `chart/templates/onepassworditem-knowledge.yaml`,
  `chart/obsidian_readiness_probe_test.sh`.
- Bump `chart/Chart.yaml` + keep `deploy/application.yaml` `targetRevision` in
  sync.

### Task 4 — Tests + plan close-out

- Update the ~19 test files that monkeypatch `VAULT_ROOT` / write temp vault
  files to assert DB state instead.
- Mark the Obsidian decommission plan Phase 6 done; Phase 7 (cancel the
  subscription) stays a manual note.

## Risk / rollback

Each task is reversible by revert. The disk write paths were already producing
nothing persistent, so behavior for end users does not regress; the visible
change is that `create_note` and the summarizer now actually persist (a fix).
Removing `/vault` also unblocks multi-replica (not enabled in this PR).
