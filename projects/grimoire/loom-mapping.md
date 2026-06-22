# Grimoire / KG on Loom: target architecture and Loom feature roadmap

> **Status:** target design + an implementable Loom feature list. This started as
> "can Loom host Grimoire's KG"; it converged on a concrete architecture
> (Iceberg/Loom as durable source of truth behind a disposable hot-tier
> projection) and a prioritized set of Loom features to build. The closing
> roadmap (§7) is the part to implement against.
>
> Loom state of record: pre-alpha, HEAD `cb70424` (2026-06-22). Ontology/ACL
> primitives cited from `src/control-plane/core/src/{ontology,acl}.rs`; Iceberg
> items cited by roadmap id from loom `docs/ROADMAP.md`.
>
> **Substrate note.** Loom is mid-migration to **Iceberg-default**
> (`fut-replace-ducklake-decision`) with a **DataFusion-native serving engine**
> (`road-iceberg-datafusion-serving`, done; no DuckDB in the read path) and
> per-column-stats predicate pushdown. Read "lakehouse" below as the Iceberg,
> Arrow, and DataFusion stack.

## 1. Purpose

Grimoire's knowledge layer and a personal KG are both "typed objects + links +
governed visibility + provenance + vector retrieval", the Foundry shape Loom is
built to be. The question is no longer _whether_ Loom expresses that, but _what to
build in Loom_ so it can be the governed durable core, with the live game served
from a fast disposable projection. This doc fixes the architecture and lists the
features.

## 2. Target architecture: Iceberg source-of-truth + disposable hot-tier projection

```
GAME START   Iceberg / Loom ──checkout (load working set)──▶ hot tier (Postgres v1 / Spanner Omni spike)
 (checkout)  governed, durable SoR                          fast reads, live mutations, pgvector, WS fan-out

DURING GAME  players / DM ──▶ hot tier   (HP, dice, feed, homebrew entities, grant flips, transcripts)
             buffer the session delta on top of the loaded base; nobody touches Iceberg

GAME END     hot tier ──check-in (one snapshot + lineage)──▶ Iceberg / Loom
 (check-in)  durable, time-travelable "state after session N"; ready for next checkout
```

- **Iceberg / Loom = governed durable system of record.** Cross-game canonical
  state, the sourcebook corpus, ontology, lineage. Not on the live path.
- **Hot tier = a swappable, disposable projection target.** Hydrated from Iceberg
  at game start, owns all live reads/writes/vector/fan-out during the session,
  discarded after check-in. Re-derivable from Iceberg, so it is a cache, not a SoR.
- **The hot tier is a component choice, not a foundational one** (see §2.3).

### 2.1 Why this dissolves the OLTP / FGAC / vector tension

The earlier worry was "Loom can't do live row-grants, can't UPDATE, can't serve
ANN." This architecture never asks it to. During the game the hot tier owns every
dynamic, mutable, low-latency thing it is good at; Loom is the bookend (load
before, commit after). So Loom's missing capabilities **re-tier** from "blockers"
to "governance/lineage features the durable layer owes its own thesis."

### 2.2 Two lifecycles, kept separate

- **Shared static corpus** (sourcebooks: entities, chunks, embeddings). Large,
  changes only on new-book ingest. A long-lived projection / index, refreshed out
  of band. Not part of the per-game checkout.
- **Per-campaign mutable state** (PCs, HP, lore, grants, transcripts, homebrew).
  Small, changes every session. This is the checkout / check-in working set.

### 2.3 The hot tier is swappable: Postgres (v1) vs Spanner Omni (spike)

Because the projection is disposable and re-derivable, the hot-tier engine is not
a one-way door, the SoR and check-in logic are unchanged by the choice.

- **Postgres + pgvector (v1, recommended floor).** Proven, one pod, seconds to
  hydrate, no preview/licensing risk. pgvector + recursive-CTE graph + tsvector
  cover every game need. For 6 users at human pace it runs at ~0.01% capacity;
  point ops are sub-millisecond and the `<200ms` budgets are network + WS fan-out,
  not DB time. Fan-out is the WS gateway (in-process / Redis pub-sub) with Postgres
  as durable persistence beside it, not the notification bus. Single-primary ACID
  is exactly the one-consistent-view-for-6-players model you want.
- **Spanner Omni (tracked spike).** Self-hostable, multi-model (relational +
  Spanner Graph/GQL + vector + KV + full-text) with cross-model ACID. Loom's
  multi-model ontology projects onto it almost 1:1 (objects→tables, links→graph,
  embeddings→vector, text→FTS) with no extensions to tune, a genuinely cleaner
  projection target. Caveats that keep it a spike, not v1: it is **Preview /
  developer edition** (no TLS, no backups, breaking changes, writes stop 90 days
  after deploy), explicitly non-production, and the only path off that is "contact
  Google." The 90-day write stop is dodgeable for an ephemeral cache (recreate
  inside 90 days; truth is in Iceberg), but the maturity / dependency posture is
  the real reason to prove it before betting on it.

Spike criteria: stand up single-node Omni, generate the Loom-ontology→Spanner
schema, confirm graph + vector + FTS work in the _preview_ build, measure checkout
latency / live throughput / check-in. If it holds, swap the projection target; the
architecture is unchanged.

### 2.4 Check-in rules (the only dangerous step)

- **Idempotent + resumable.** Key the commit by session id; a retry is a no-op if
  that session already committed.
- **Keep the hot tier until Iceberg acknowledges.** Do not tear down on "game
  over"; the hot tier is the durable WAL until check-in confirms, or a failed
  flush loses the session.
- **Single-active-session invariant makes it conflict-free.** Grimoire already
  enforces "at most one active/paused session per campaign", so no two games check
  out the same campaign concurrently, no write-conflict on check-in, no
  distributed locking needed.
- **Append the immutable, upsert the mutable, one snapshot per game.** Transcripts
  and events append; HP / grant status / world state upsert; commit the whole
  delta as one Iceberg snapshot with "session N" lineage, which buys time-travel
  to any past campaign state.

## 3. Data model mapping (Loom ontology)

### 3.1 Entity -> ObjectType (one type per `entity_type`)

Grimoire's polymorphic `Entity` (jsonb `properties`) maps to **one typed
`ObjectType` per entity_type**, each backed by its own Iceberg table with columns
shredded from the jsonb: `Creature`, `Spell`, `Location`, `NPC`, `Faction`,
`Deity`, `Item`. Better modeling (real types, column policy, stats) at two costs:
homebrew/unknown shapes need a `define_type` upsert rather than a free jsonb row,
and ingest must shred structured extraction into per-type columnar Parquet (a
Transform, §4). Nested arrays (`actions[]`, `ability_scores{}`) become struct
columns or child types, open modeling question (§9).

### 3.2 Relationship -> LinkDef (the cleanest mapping)

Every `rel_type` is a `LinkDef`. The generic edge table maps to
`LinkBacking::JoinTable` (one mapping table, `rel_type` selecting the link);
symmetric relations (`ALLIED_WITH`, `HOSTILE_TO`) use `reversed()`. See §6 for
traversal, which Loom already serves.

### 3.3 KnowledgeChunk + ChunkEntityMention -> types + links

`KnowledgeChunk` is an ObjectType (with an embedding column, §5);
`ChunkEntityMention` is a `Chunk <-> Entity` join-table link carrying
`mention_span` / `context` on the backing table. Grant-ratio and
first-mention-drop chunk filtering is application logic above the store.

### 3.4 KnowledgeGrant -> ACL scope (+ FGAC for the which-entities axis)

Two axes: **scope** (`full` / `partial` / `name_only`) maps cleanly to column
policy (three roles, different `deny_columns` / `mask_columns`); **which entities a
subject may see** does NOT, because Loom's `RowFilter` is literal-only and cannot
join an entitlement table on the current subject. In this architecture that axis
is enforced in the **hot tier during play** (the grant table lives in Postgres,
gating queries), so FGAC is only required if players read Loom _directly_ between
games, or multi-tenant (see L7, §7).

### 3.5 Operational tables -> hot tier only

`Session`, `SessionEvent`, `SessionTranscript`, live character state, dice, feed,
`QueryLog`: OLTP + realtime, they live in the hot tier and check in as part of the
session delta. Never on Loom's live path.

## 4. Enrichment via Transforms

A Loom Transform is a queue-driven DataFusion job: read snapshots, run a logical
plan (including a model call), write a new snapshot + lineage. This is the home
for Grimoire's whole offline pipeline:

| Verb    | Transform                         | Pipeline                                              |
| ------- | --------------------------------- | ----------------------------------------------------- |
| Enrich  | compute new columns               | embeddings, LLM structured extraction, derived fields |
| Modify  | rewrite / canonicalize rows       | entity resolution / alias-merge, cleaning, dedup      |
| Connect | join across types, emit edge rows | build Relationship, ChunkEntityMention, cross-refs    |

The extract → resolve → chunk → embed → link DAG is a chain of Transforms, each
enqueuing the next, each committing with lineage. Run them at **ingest** (new
book) and at **post-game check-in** (embed homebrew, resolve against canon,
extract relationships from transcripts). The model/embedding call inside a
Transform is **application code**; Loom provides the read/write/lineage/queue
scaffolding. Transforms are batch and durable, never the in-game path (that is the
hot tier). Status: queue + worker + DataFusion compute built;
**Transform-output-to-Iceberg is planned** (`road-iceberg-transform-writes`).

## 5. Vectors

Three separable concerns, different homes:

1. **Store** the embedding as an Iceberg column (Arrow `FixedSizeList<Float32, N>`),
   governed and lineage-tracked like any column. This is the **truth**.
2. **Generate** it with an embedding **Transform** (§4): every vector records its
   model version and source snapshot, governed, reproducible embeddings.
3. **Serve** ANN from the hot tier: a per-game **pgvector** index built at
   checkout (or Spanner-native vector if that tier). At a campaign's scale this
   build is trivial. New embeddings created in-game flow back via the Transform on
   check-in.

So Loom owns storage + generation + lineage; it is **not** an ANN engine, by
design.

### 5.1 Governance seam (only if reading vectors from Loom directly)

If a between-game flow searches Loom's vectors directly, the index must be an
**unprivileged candidate generator** (vectors + IDs only) with the per-subject
grant resolved on **governed hydration** back through Loom (over-fetch K' > K,
drop ungranted, refill). This leans on FGAC (L7). Inside a game this never
arises, the hot tier holds the grants and the vectors together.

### 5.2 Landscape (2026): why the serving index is external

- **Native Iceberg vector type/index: no.** The community proposal
  ([apache/iceberg #12636](https://github.com/apache/iceberg/issues/12636)) was
  closed as not planned; v3 added variant/geometry/geography, not vector.
- **AWS S3 Vectors: a companion store, AWS-only.** A separate vector-bucket type
  paired with S3 Tables (managed Iceberg), not a native in-Iceberg index, and not
  available on self-hosted S3 (SeaweedFS). It validates the "derived index
  materialised from the lakehouse" pattern; it is not a tool we can run.
- **Puffin-backed vector indexes: the future in-chokepoint path.** Recent research
  ([arXiv 2606.04196](https://arxiv.org/abs/2606.04196)) attaches an ANN index
  (HNSW / IVF-PQ / Vamana) as a **Puffin sidecar bound to an Iceberg snapshot**.
  This is the only approach that pulls the index _inside_ the governed table; if
  Loom's DataFusion serving engine learned to read it, vector search would run in
  the chokepoint and FGAC would apply in the same query (collapsing §5.1's
  two-step). Track it; not buildable yet.

### 5.3 Optional: self-hosted S3 index object (between-game)

For between-game ANN without the hot tier, a free-standing index object on S3
works under eventual consistency: build over snapshot `S_idx`, serialize to an
immutable key, flip a one-key `CURRENT` pointer (atomic), load + cache at query
time, ANN returns entity ids, hydrate through Loom. The live read self-heals
deletions (truth is the live snapshot); only brand-new rows lag until rebuild. The
minimal metadata is that one pointer, which Puffin (§5.2) later subsumes. Low
priority, pgvector covers in-game; this is only for prep browsing.

## 6. Graph: recursive joins, no graph engine needed

The graph is an **edge table** and traversal is **recursive / chained SQL joins**.
Loom already implements and tests depth-bounded recursive-CTE traversal over its
Iceberg/DataFusion engine, both FK- and join-table-backed (the fixed
`iss-recursive-cte-iceberg`, asserting reachable sets over real Parquet). This
matches Grimoire's own decision ("1-2 hop JOINs, no Neo4j"). Two shapes, both
cheap: heterogeneous shallow patterns (NPC→Faction→Location) are fixed join chains;
homogeneous deep traversal (Location CONTAINS…) is the recursive CTE Loom built.
At game time the same runs in the hot tier. **No graph engine, no "Iceberg graph
support" to add.** Escape hatch if deep/algorithmic graph ever appears: a graph
_query engine over the lakehouse_ (PuppyGraph over Iceberg, Apache GraphAr), not a
separate graph database, overkill for 1-3 hop lore.

## 7. Loom feature roadmap (build against this)

Tiers are dependency-ordered. "Loom status" is `done` / `planned` (existing
roadmap id) / `new` (propose as a new item). Nothing here asks Loom to be an ANN
or graph engine.

### Tier 1 — durable substrate (unblocks the architecture)

| id  | Feature                                                 | Why (this architecture)                                          | Loom status                                                                            | Depends              |
| --- | ------------------------------------------------------- | ---------------------------------------------------------------- | -------------------------------------------------------------------------------------- | -------------------- |
| L1  | **Iceberg overwrite / replace write mode**              | any mutable state: entity upserts on check-in, transform replace | planned `road-iceberg-overwrite-mode`                                                  | none (append exists) |
| L2  | **Transform output to Iceberg**                         | the enrichment DAG engine (extract/resolve/chunk/embed/link)     | planned `road-iceberg-transform-writes`                                                | L1                   |
| L3  | **Vector column type + embedding-generation Transform** | embeddings as governed, lineage-tracked data                     | **new**                                                                                | L2                   |
| L4  | **Governed bulk checkout export (Arrow Flight)**        | hydrate the hot tier fast, columnar                              | data plane planned `road-engine-wire-flight`; governed bulk-read export **new** on top | client wire          |

### Tier 2 — governed write-back (check-in)

| id  | Feature                                | Why                                                                      | Loom status                                                       | Depends |
| --- | -------------------------------------- | ------------------------------------------------------------------------ | ----------------------------------------------------------------- | ------- |
| L5  | **Governed UPDATE / DELETE actions**   | check-in upserts mutable entity state; durable layer reflects post-game  | `road-iceberg-actionengine` is insert-only; UPDATE/DELETE **new** | L1      |
| L6  | **Per-session atomic check-in commit** | one snapshot + lineage per game = time-travel to "state after session N" | **new** (composes snapshot+lineage, built, with L1 + append)      | L1, L5  |

### Tier 3 — governance maturity (direct / player / multi-tenant reads; not v1)

| id  | Feature                                                    | Why                                                                                             | Loom status                       | Depends            |
| --- | ---------------------------------------------------------- | ----------------------------------------------------------------------------------------------- | --------------------------------- | ------------------ |
| L7  | **FGAC: subject-attribute + entitlement-join `RowFilter`** | per-subject reads if players read Loom directly, or multi-tenant; the general-platform keystone | **new** (acl is P4, literal-only) | none (independent) |

### Already there — no work

- Recursive-CTE graph traversal, FK + join-table (`iss-recursive-cte-iceberg`,
  fixed) and single-hop links.
- Atomic snapshot + lineage commit (ingest path).
- DataFusion-native Iceberg serving + per-column-stats predicate pushdown
  (`road-iceberg-datafusion-serving`, `road-iceberg-percolumn-stats`, done).

### Explicitly not building in Loom

- An **ANN engine** (serve from external pgvector / Spanner-native; future Puffin
  in-chokepoint).
- A **native property-graph engine** (recursive joins suffice).

### Suggested build order

`L1` first (unblocks L2 and L5). Then `L2 → L3` (enrichment + embeddings) and
`L5 → L6` (write-back) can proceed in parallel. `L4` follows the client wire. `L7`
is independent and only gates direct/player/multi-tenant reads, defer past v1
unless a between-game player-facing read of Loom is wanted early.

## 8. What stays out of Loom (owned by the hot tier / external)

Live OLTP and sub-second fan-out (hot tier + WS gateway), ANN serving (pgvector /
Spanner-native), the graph traversal engine (recursive SQL in either tier),
per-subject grant enforcement _during play_ (hot-tier grant table).

## 9. Open questions

- **Nested values** (§3.1): struct columns vs child ObjectTypes for
  `actions[]` / `ability_scores{}`.
- **Check-in merge** (§2.4): exact upsert-vs-append split per table, and whether
  the whole campaign working set overwrites or only the diff commits.
- **FGAC priority** (L7): is any between-game player-facing read of Loom wanted in
  v1, or does the hot tier own all player gating indefinitely?
- **Hot-tier choice** (§2.3): outcome of the Spanner Omni spike vs Postgres v1.
- **Embedding model + dim** in the Transform (L3), and whether vector lineage
  records enough to reproduce.
