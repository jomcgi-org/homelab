# MCP + Remote-Routine Gardener: Architecture Map & Plan

**Design for:** [ADR 006 Phase 4](../decisions/platform/006-obsidian-decommission-postgres-interim.md) (gardener execution model)
**Created:** 2026-06-16
**Supersedes:** the in-pod `knowledge` Typer CLI gardener (4a-i, shipped) and the per-run-tmpdir design.

---

## Premise

Note bodies are authoritative in `knowledge.notes.content` (Postgres), raws are in `s3://knowledge/raws/<hash>.md`, Obsidian + the vault filesystem are gone, and the gardener is offline. "New notes are no longer new files," so ingestion and gardening are now Postgres/MCP events, not filesystem events. The gardener becomes **scheduled claude.ai routines over MCP**, not an in-pod subprocess (a remote routine reaches the cluster only over the network, so MCP is the only viable interface; the CLI is retired).

---

## 1. Ingestion + web-app edits (file-less)

**Raw in (all sources write directly to Postgres + S3, no files):**

- Each ingestion source (URL fetch via `ingest_queue`, the capture box, Discord mirror, `web_share`, `insert_api`) inserts a `knowledge.raw_inputs` row and uploads the blob to `s3://knowledge/raws/<content_hash>.md` in one unit. Content lives in S3; `raw_inputs` holds metadata + `content_hash`.
- **Retire** the filesystem ingestion machinery: `raw_ingest.move_phase`, `reconcile_raw_phase`, vault-root discovery, and `ingest_queue._write_raw_md`'s file write. These exist only because raws used to arrive as vault files.
- "Needs decomposition" is unchanged in concept: a `raw_inputs` row with no current-version `atom_raw_provenance` (fresh) or a `failed` row with `retry_count < 3` (retriable). `no-new-notes` is the success-but-empty sentinel.

**Web-app edits trigger MCP, not files:**

- Edit an existing atom -> `edit_note` (already re-indexes synchronously into Postgres + chunks).
- Capture box (new raw) -> insert `raw_inputs` (+S3); the gardener decomposes it later.
- Direct new atom (rare) -> `create_atom`.

## 2. Three routines, each a cron + Skill + the `homelab` MCP connector

| Routine                 | Cadence | Skill                   | MCP tools used                                                                                                                                             |
| ----------------------- | ------- | ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `knowledge-gardener`    | hourly  | `knowledge-gardener`    | `list_raws_needing_decomposition`, `get_raw`, `search_knowledge`, `create_atom`, `edit_note`, `patch_edges`, `record_provenance`, `acquire_lock`, `notify` |
| `knowledge-distill`     | daily   | `knowledge-distill`     | `list_tasks`(done), `get_note`, `create_atom`, `record_provenance`                                                                                         |
| `knowledge-consolidate` | daily   | `knowledge-consolidate` | `list_tasks`, `create_atom`/`edit_note`                                                                                                                    |

Routine YAML lives in `projects/monolith/claude_routines/`, synced by `/update-claude-routines`; `homelab` connector -> the monolith MCP gateway. The routine `prompt` invokes its Skill.

## 3. Guardrails in two layers (stronger than today)

- **Soft (Skill):** the full `_CLAUDE_PROMPT_HEADER` content moves verbatim into the `knowledge-gardener` Skill: atom/fact/active definitions, the `visibility.py` criteria, aliases rules, wikilink do/don't, edge types, the `active` size guide. `GARDENER_VERSION` becomes the skill version stamped into provenance. `_DISTILL_PROMPT` -> the distill Skill.
- **Hard (MCP boundary):** `create_atom` validates server-side, `type in {atom,fact,active}`; `active` requires `status`+`size`; `visibility` required; edges typed against the known set; `derived_from_raw` required, and rejects malformed atoms with a correctable error. Today only the prompt guards, so this is strictly stronger. (This validation logic is lifted from the retired `cli.py` `create-atom`.)

## 4. Limits

- **Cadence:** hourly (the routine API's 1h minimum).
- **Per-session batch:** `list_raws_needing_decomposition(limit=N)` caps each run (the `GARDENER_MAX_FILES_PER_RUN` concept; start N=5-10) to bound session length and cost.
- **No overlap:** `acquire_lock("knowledge.garden", ttl)` at the top of each run; skip if held.
- **Retry/sentinel:** keep `failed`/`retry_count<3` + `no-new-notes`, enforced in `record_provenance` + `list_raws_needing_decomposition`.
- **Cost:** remote routines are free under the Claude Max sub, so the bound is session-count (hourly), not tokens.

## 5. Web editor (future, low priority)

Deferred. Reads via `get_note`/`search_knowledge`; writes via `edit_note` + a capture box that inserts `raw_inputs`. No new gardener dependency.

---

## Build sequence

- **4c-1 - Knowledge MCP tools** (`knowledge/mcp.py`): add `list_raws_needing_decomposition`, `get_raw` (reads `raw_inputs.content` now, S3 later), `create_atom` (schema-enforced + `index_note_from_raw`), `patch_edges`, `record_provenance`. Lift validation from `cli.py`. Tests.
- **4c-2 - Skills**: `knowledge-gardener`, `knowledge-distill`, `knowledge-consolidate` SKILL.md, carrying the migrated prompts/guardrails.
- **4c-3 - Routines**: three YAMLs in `claude_routines/`, synced via `/update-claude-routines`; verify a manual run end-to-end; then enable hourly/daily.
- **4c-4 - File-less ingestion**: rewrite the ingestion sources to write `raw_inputs` + S3 directly; delete `move_phase`/`reconcile_raw_phase`/vault discovery.
- **4c-5 - Retire the in-pod gardener + CLI**: delete `gardener.py`'s subprocess path, `knowledge/cli.py`, `knowledge/tools/knowledge`, the `knowledge-search` shim (superseded by `search_knowledge` MCP), the gardener scheduled job; remove `@pip//typer` if unused.
- **4d - Drop `raw_inputs.content`**: point `get_raw` at S3, then drop the column + `VACUUM` (streaming, never `.all()` in the serving pod).
- **Phase 6 leftover**: retire the reconciler disk scan, drop the `/vault` emptyDir, delete the `obsidian-image` build target + knowledge `OnePasswordItem`.

## Out of scope / deferred

Web editor (Phase 5). COSI bucket provisioning (revisit at seaweedfs chart upgrade; `s3.bucket.create` is verified idempotent so adoption is safe later).
