# Obsidian Decommission: Implementation Plan

**Design:** [ADR 006 — Decommission Obsidian via a Postgres Interim](../decisions/platform/006-obsidian-decommission-postgres-interim.md)
**Created:** 2026-06-13
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

## Phase 0 — Accept ADR 006

- Flip ADR 006 status `Draft` -> `Accepted` (`docs/decisions/platform/006-obsidian-decommission-postgres-interim.md:4`). This is Joe's call; the plan assumes acceptance.

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

## Phase 4 — Gardener without a durable vault FS

Repoint the gardener's Claude Code subprocess from the durable `/vault` to a **per-run tmpdir** materialized from Postgres:

- `knowledge/gardener.py:261-277` (`vault_root`/`processed_root`), `:786` (`cwd=self.vault_root`), `:920-984` (before/after glob + read-back): materialize only the working set (target raws + relevant `_processed` notes pulled from `knowledge.notes.content`) into `tempfile.mkdtemp()`, set `cwd` there, diff new/changed files after the run, and `upsert_note` results back to Postgres keyed on `note_id` + `content_hash`.
- The vault-wide reconciler scan becomes redundant for gardener output once writes go through `upsert_note`; keep it running until Phase 6 as the consistency net.

**Verify:** CI green; trigger `knowledge.garden` (scheduler skill / `monolith-agent-trigger-job`), confirm new atoms land in Postgres with no durable-vault dependency.

## Phase 5 — Notes web UI (editor + search)

Reuse what exists, add the write surface. Existing: `/private/notes` graph + read-only `NotePanel` + `GraphSearch` (`frontend/src/lib/components/notes/`), and `GET /api/knowledge/search` + `/notes/{id}` (`router.py:84-109, 377-393`).

- Add a markdown **editor** component + create/edit/delete wiring against the Phase 3 endpoints, behind Cloudflare Access (same auth path as the rest of `/private`).
- Add a **capture box** for quick mobile capture (writes to `ingest_queue`).
- List + search views reuse the existing search API. **Graph navigation in the editor is deferred** (the standalone read-only graph stays).

**Verify:** CI green; manual `/verify` against the deployed `/private/notes` editor — create, edit, search, delete round-trip through Postgres; works on mobile browser.

## Phase 6 — Retire Obsidian plumbing (the cutover)

Only after Phase 5 is the validated daily driver:

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
- **Web UI partly redone at ADR 004 (lakehouse)** -> data access stays behind the `KnowledgeStore` interface so the read path re-points at Quack rather than being rewritten.

## Out of scope

ADR 004 lakehouse cutover (NATS/Temporal/Iceberg/Quack). This plan keeps the note/gap representation shape-compatible with `note_events`/`gap_events` so that later cutover is a re-point, not a remodel.
