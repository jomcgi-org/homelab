# Obsidian Decommission: Implementation Plan

**Design:** [ADR 006 — Decommission Obsidian, Postgres as the Body of Record](../decisions/platform/006-obsidian-decommission-postgres-interim.md)
**Created:** 2026-06-13
**Updated:** 2026-06-18: **Phase 6 complete** (vault-to-Postgres cutover, see `2026-06-18-vault-to-postgres-cutover.md`). Phases 1-4 shipped earlier; **Phase 5 (web editor) deferred** by decision (note editing happens via the MCP tools and the existing HTTP endpoints; a dedicated `/private/notes` editor was judged not needed now). Phase 7 (cancel the Obsidian Sync subscription) remains a manual out-of-band step. Earlier: 2026-06-14 ADR 006 Accepted; Phase 1 merged (#2593, chart 0.135.0); Phase 2 (#2604).
**Branch:** `claude/obsidian-postgres-migration-b6sdnl`
**Scope decision:** Full ADR 006 end-to-end. v1 web UI ships editor + search; wikilink graph navigation is deferred to a follow-up phase (the read-only graph at `/private/notes` already exists and stays).

---

## Premise correction (read first)

The note bodies are **not yet authoritatively in Postgres**. `knowledge.notes` stores only `content_hash` (`projects/monolith/knowledge/models.py:87`); there is no `content` column (`chart/migrations/20260408000000_knowledge_schema.sql:12-32`). The body text exists in Postgres only as derived, lossy section chunks (`knowledge.chunks.chunk_text`). The round-trippable markdown still lives on the `/vault/_processed` emptyDir and is mirrored to Obsidian cloud by the `headless-sync` sidecar.

So **Phase 1 (body into Postgres) is the gate** for everything else. Nothing about Obsidian can be safely removed until the body of record has moved off disk and been validated.

The deployment is already `strategy: RollingUpdate` (`deployment.yaml:14`), but it is effectively single-instance because `/vault` is an `emptyDir` and the backend + obsidian sidecar are coordinated disk writers. Dropping the volume (Phase 6) is what actually unlocks N replicas, not a strategy change.

---

## Sequencing principle

Each phase leaves the system **shippable and reversible**, with Obsidian still running as a safety net until Phase 6. We dual-write (disk + Postgres) through Phases 1-5 so a rollback is always "read from disk again," then cut the disk dependency only once the web UI is the validated daily driver.

Per repo rules: no local test loop. Each phase is a commit (or small commit set) pushed to the branch; verification is the PR's BuildBuddy CI run. Backfill data must **not** go in a migration (256 KiB `last-applied-configuration` cap, per CLAUDE.md anti-pattern), so it runs as a code pass, not SQL seed.

---

## Phase 0 — Accept ADR 006 ✅ (2026-06-14)

- ADR 006 status flipped `Draft` -> `Accepted` and reframed from "interim" to "destination" after the ADR 004 lakehouse was decommissioned (PR #2596). ADR 004 marked `Superseded` for the KG storage domain.

## Phase 1 — Note body in Postgres (the gate)

**Goal:** `knowledge.notes.content` becomes the authoritative body; reconciler and write paths populate it; existing disk reads stay as fallback.

1. **Migration** `chart/migrations/20260613000000_knowledge_notes_content_column.sql`: `ALTER TABLE knowledge.notes ADD COLUMN content TEXT;` (nullable initially so existing rows are valid until backfilled).
2. **Model** `knowledge/models.py:67` (`Note`): add `content: str | None = None`. Keep `content_hash`.
3. **Store** `knowledge/store.py` `upsert_note`: accept and persist `content` (the authored body, frontmatter stripped). Keep computing/storing `content_hash` from that same body.
4. **Reconciler** `knowledge/reconciler.py:257-352`: pass the parsed `body` (already available at `_ingest_one`, line ~281) into `upsert_note(content=body, ...)`. No behavior change yet beyond populating the column.
5. **Backfill = one full reconcile pass.** Because the reconciler reads disk and now writes `content`, a single full scan backfills every existing note. Add a one-shot "force full" path (ignore `content_hash` skip) or simply let the next scheduled reconcile populate nulls. Verify: `SELECT count(*) FROM knowledge.notes WHERE content IS NULL` -> 0 after the pass.

**Verify:** CI green; after deploy, query confirms no null `content`. Obsidian untouched.

## Phase 2 — Read paths off disk

Switch every body read from `(vault_root / path).read_text()` to `note.content`, with a disk fallback while any `content` may still be null:

- `knowledge/mcp.py` `get_note` (line ~92), `edit_note` read (line ~190)
- `knowledge/router.py` `get_knowledge_note` (line ~393), `get_public_note` (line ~317)
- `knowledge/notes.py` `_read_note_snippet` (line ~78, review-queue)

**Verify:** CI green; `mcp__homelab__monolith-get-note` and the `/private/notes` panel return bodies sourced from Postgres (spot-check a note edited only in DB).

## Phase 3 — Write paths to Postgres (dual-write)

Make creates/edits/deletes write `content` to Postgres as the source of truth, and re-chunk + re-embed on write (lifting the chunk/embed step out of the reconciler so it runs synchronously on the write path):

- `knowledge/mcp.py` `create_note` (line ~148), `edit_note` (line ~235), `delete_note`
- `knowledge/router.py` `create_note` (line ~571), `edit_note` (line ~499), `delete_note_endpoint`

Keep writing the disk file **too** (dual-write) so Obsidian Sync and the reconciler stay consistent as a safety net through Phase 5. Soft-delete already tracks `deleted_at` / `pre_delete_path` (`models.py:123-133`) — keep that, just drive it from the DB write.

**Verify:** CI green; create/edit via web API, confirm row + chunks update and (still) a disk file appears.

## Phase 4 — Gardener via schema-enforced tools (revised 2026-06-14)

Originally specified as a per-run tmpdir materialized from Postgres. Superseded by a **tool-based gardener**: the subprocess never touches a filesystem, it operates through a `knowledge` Typer CLI grounded in the monolith store. This dissolves the materialization problem (the subprocess discovers and edits notes mid-run via `knowledge-search`, so the working set is unknowable up front) and folds in schema enforcement. Split into two independently shippable sub-phases.

### Phase 4a — `knowledge` Typer CLI + tool-based gardener

- **New `knowledge/cli.py`**: a Typer app exposing `search`, `get`, `create-atom`, `edit`, `patch-edges`, `get-raw`. Thin veneer over `KnowledgeStore` + `knowledge.indexing` (the same code the HTTP API and MCP use); opens a DB session in-process exactly as `knowledge/tools/knowledge-search` does today. Register a console-script/shim and bake into the image alongside `knowledge-search` (`BUILD:173-175` pattern).
- **Schema enforcement**: Pydantic models for `atom` / `fact` / `active` (active requires `status` + `size`; edges typed; `visibility` required). `create-atom` validates and, on failure, prints a correctable error the subprocess can act on. `create-atom`/`edit` go through `index_note_from_raw`/`index_parsed_note` (chunk + embed + upsert, committed) and record `atom_raw_provenance`.
- **Gardener rewrite** (`knowledge/gardener.py`): drop `cwd=vault_root`, the before/after `_processed` glob, and the disk read-back. Rewrite `_CLAUDE_PROMPT_HEADER` to instruct the subprocess to use the `knowledge` CLI verbs instead of Read/Write/Edit + filesystem paths. Provenance is recorded from the `create-atom` return (note ids), not from globbed files.
- **Verify:** CI green; trigger `knowledge.garden`, confirm new atoms land in Postgres with no `_processed` filesystem writes; malformed-atom rejection exercised in tests.

### Phase 4b — Raws to SeaweedFS via COSI (sequenced after 4a)

- Provision a `knowledge-raws` bucket via a COSI `BucketClaim` in `projects/monolith/deploy/` per [ADR 007](../decisions/platform/007-seaweedfs-bucket-provisioning-cosi.md) (`deletionPolicy: Retain`, content-addressed, no TTL).
- Move raw content from `knowledge.raw_inputs.content` (Postgres) to `s3://knowledge-raws/<sha256>`, mirroring the `chat.blobs` migration (nullable -> backfill/export -> drop column -> `VACUUM FULL`). `raw_inputs` keeps metadata + the content-hash key. `get-raw` reads from S3.
- **Verify:** CI green; `raw_inputs.content` dropped; gardener `get-raw` serves from SeaweedFS; Postgres size reclaimed.

## Phase 5 — Notes web UI (editor + search) — DEFERRED (2026-06-18)

Deferred by decision: note editing is handled via the MCP `create_note` /
`edit_note` / `delete_note` tools and the existing HTTP CRUD endpoints, so a
dedicated `/private/notes` editor was judged not worth building now. Phase 6
went ahead without it (the disk write paths were already producing nothing
persistent, so there was no daily-driver gate to satisfy first).

Reuse what exists, add the write surface. Existing: `/private/notes` graph + read-only `NotePanel` + `GraphSearch` (`frontend/src/lib/components/notes/`), and `GET /api/knowledge/search` + `/notes/{id}` (`router.py:84-109, 377-393`).

- Add a markdown **editor** component + create/edit/delete wiring against the Phase 3 endpoints, behind Cloudflare Access (same auth path as the rest of `/private`).
- Add a **capture box** for quick mobile capture (writes to `ingest_queue`).
- List + search views reuse the existing search API. **Graph navigation in the editor is deferred** (the standalone read-only graph stays).

**Verify:** CI green; manual `/verify` against the deployed `/private/notes` editor — create, edit, search, delete round-trip through Postgres; works on mobile browser.

## Phase 6 — Retire Obsidian plumbing (the cutover) — DONE (2026-06-18)

Shipped in the vault-to-Postgres cutover (`2026-06-18-vault-to-postgres-cutover.md`).
What actually landed (the runtime was already Obsidian-disabled since 2026-06-15,
and the reconciler + vault-backup/reconcile jobs were already removed in #2680):

- Note read/write paths are DB-only: `create_note`/`edit_note`/`delete_note`/
  visibility/undelete no longer touch disk; both `edit_note` endpoints share one
  `reindex_note_with_edits` core that reconstructs frontmatter from the row.
- Removed the `obsidian` sidecar, the `vault` emptyDir + mounts, `VAULT_ROOT` /
  `VAULT_API_URL` / `VAULT_GIT_REMOTE` env, the obsidian-image, the obsidian
  creds `OnePasswordItem`, the readiness-probe test, and the
  `headlessSync`/`vault`/`gitRemote`/`onepassword` values.
- Dropped the Discord `vault_export` summary mirror (summaries persist as DB
  rows regardless).
- `/vault` removal unblocks N replicas (left at 1 here).

Original checklist (for reference):

1. Drop disk dual-write from Phases 3-4 (Postgres-only).
2. Remove the `obsidian` sidecar container (`deployment.yaml:207-237`), the `vault` emptyDir volume (`:264-268`) and its mounts (`:192-201`, `:232-234`), and the obsidian readiness probe.
3. Remove `knowledge.headlessSync`, `knowledge.gitRemote`, and the `onepassword: obsidian` item from `deploy/values.yaml:74-80` and `chart/values.yaml`; delete the now-unused `OnePasswordItem` for the obsidian token.
4. Retire the reconciler's disk scan (it has no source once `/vault` is gone). Keep chunk/embed on the write path (Phase 3).
5. Replace the `knowledge.vault-backup` git-push job (`service.py:115-149, 579-584`) with reliance on CNPG WAL/PITR (already configured) plus an **optional** periodic markdown export to a read-only git mirror.
6. Confirm N replicas now work (volume gone); bump `backend.replicas` if desired.
7. Remove `projects/monolith/obsidian-image/` and its BUILD/image targets, and the `obsidian_readiness_probe_test.sh`.

**Verify:** CI green; ArgoCD syncs; pod has no obsidian sidecar/volume; `/private/notes` editor fully functional; rollout healthy at >1 replica.

## Phase 7 — Cancel the subscription (manual)

Out-of-band: cancel the paid Obsidian Sync subscription and archive the `jomcgi/obsidian-vault` git mirror as read-only. Not a code change; tracked here for completeness.

---

## Risks (from ADR 006, with plan-level mitigations)

- **Backfill incomplete** -> Phase 1 gate asserts `content IS NULL` count is 0 before any read/write switch.
- **Gardener tmpdir mis-writes** -> upsert keyed on `note_id` + `content_hash`; optional markdown git export as recovery; dual-write kept until Phase 6.
- **Lost mobile capture** -> capture box + `ingest_queue` validated in Phase 5 before Sync is cut in Phase 7.
- **Web UI is net-new work** -> reuse the existing `/private/notes` graph + search API; data access stays behind the `KnowledgeStore` interface for testability and a clean read-path seam.

## Out of scope

The ADR 004 lakehouse cutover is no longer pending: that stack was decommissioned on 2026-06-14 (PR #2596). Postgres is the destination, so there is no later re-point to design around and no `note_events`/`gap_events` shape to mirror.
