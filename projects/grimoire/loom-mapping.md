# Grimoire / KG on Loom: target architecture and implementation plan

> **Status: Loom side complete, Grimoire implementation can begin.** This started
> as "can Loom host Grimoire's KG"; it converged on a concrete architecture
> (Iceberg/Loom as durable source of truth behind a disposable hot-tier
> projection). Every capability this design asked Loom for has since **shipped or
> dissolved** (loom PR #268, reconciled 2026-06-30): the durable substrate is
> ready and Grimoire needs **no new loom code**. The doc has flipped accordingly,
> from a Loom feature roadmap to the Grimoire-side build. §7 is now the
> implementation plan (composing shipped primitives), not a wishlist.
>
> Loom state of record: the eight-item ask (A1-A7 in loom
> `docs/grimoire-kg-agenda.md`) is fully accounted for. A1-A5 shipped; A6 (atomic
> per-session check-in) and A7 (fine-grained ACL) **dissolved** into the
> dataset-partition model (§3.4). Ontology/ACL primitives cited from
> `src/control-plane/core/src/{ontology,acl}.rs`; Iceberg items cited by shipped
> roadmap id.
>
> **Substrate note.** Loom is Iceberg-default with a **DataFusion-native serving
> engine** (`road-iceberg-datafusion-serving`, done; no DuckDB in the read path)
> and per-column-stats predicate pushdown. It has since also grown **engine-side
> vector indexes** (Puffin exact + IVF-Flat + HNSW) and an external `/search` kNN
> endpoint (`road-puffin-vector-index` / `road-ivf-vector-index` /
> `road-hnsw-vector-index` / `road-vector-search-endpoint`), more than this design
> asked for. Grimoire still serves ANN from its hot tier by choice (§5); the
> loom-native path is now available should that ever change. Read "lakehouse"
> below as the Iceberg, Arrow, and DataFusion stack.

## 1. Purpose

Grimoire's knowledge layer and a personal KG are both "typed objects + links +
governed visibility + provenance + vector retrieval", the Foundry shape Loom is
built to be. Two questions are now both settled: Loom _can_ express that, and Loom
_already ships_ every primitive this design needs (§7). The remaining question is
purely Grimoire-side: **how to build the checkout / play / check-in loop against
ready Loom**, with the durable core in Iceberg and the live game served from a
fast disposable projection. This doc fixes the architecture (§2-§6), records the
loom-readiness ledger (§7), and lays out the Grimoire build (§7.1) and the open
implementation decisions (§9).

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
before, commit after). So Loom's then-missing capabilities **re-tiered** from
"blockers" to "governance/lineage features the durable layer owes its own thesis",
and have since either shipped (UPDATE/DELETE) or dissolved: the one that looked
hardest, per-subject visibility, never needed fine-grained row policy at all. It is
served by **dataset partitioning** (§3.4) plus the hot-tier merge, using Loom's
already-shipped coarse `Read` grant. Nothing on this list remains open on the Loom
side.

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

### 2.4 Check-in rules (external orchestration over shipped primitives)

Check-in needs **no atomic multi-dataset commit primitive** from Loom (this was
ask A6; it dissolved, PR #268). Because Iceberg snapshots are immutable and
append-only, a partial cross-dataset flush **cannot corrupt** the durable state
(the prior consistent snapshot stays intact) and an external retry-until-complete
heals any lag. So check-in is external orchestration over per-dataset versioned
writes, governed by these rules:

- **Idempotent + resumable.** Key the commit by session id; a retry is a no-op if
  that session already committed. This retry loop, not a Loom transaction, is what
  makes the multi-dataset flush consistent.
- **Keep the hot tier until every dataset is acknowledged.** Do not tear down on
  "game over"; the hot tier is the durable WAL until check-in confirms all
  datasets, or a failed flush loses the session.
- **Single-active-session invariant makes it conflict-free.** Grimoire already
  enforces "at most one active/paused session per campaign", so no two games check
  out the same campaign concurrently, no write-conflict on check-in, no
  distributed locking needed.
- **Append the immutable, replace the mutable, one snapshot per dataset per game.**
  Transcripts and events append; HP / grant status / world state go back via the
  shipped governed UPDATE/DELETE (`road-update-delete-actions`, #208) or a
  fresh per-session dataset snapshot. Per-dataset snapshots give **time-travel to
  "state after session N"** ("what did the party know then") for free.
- **Optional nicety, not a requirement:** a first-class full-state _replace_ ingest
  endpoint (`fut-ingest-overwrite-endpoint`, deferred) would let check-in write one
  stable dataset name with Iceberg-native snapshot versioning. Until then check-in
  works over the append path + external orchestration. See §9.

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

### 3.4 KnowledgeGrant -> dataset partitioning + coarse per-dataset `Read`

Per-player visibility looked like it needed fine-grained ACL (ask A7:
subject-attribute `RowFilter` + entitlement-join). Working the domain to the ground
(loom PR #268) **dissolved that**: at Grimoire's scale (a fixed ~6-profile party +
DM, ~thousands of nodes) visibility is **dataset partitioning, not row policy**.

- **Partition the graph into datasets.** A `global` dataset (facts every PC knows /
  shared world state) plus a **per-character dataset** `facts_<player>` (what that
  PC has been granted). A player's view is `global ∪ facts_<player>`, **merged in
  the hot tier** at query time. The DM tier is a single unfiltered `Read` over all
  datasets.
- **Govern at the dataset level with the shipped coarse grant.** Grant a subject
  `Action::Read` on `facts_global` + their own `facts_<player>`, deny the rest.
  **No `RowFilter` extension is needed**: Loom's literal-only ACL
  (`control-plane/core/src/acl.rs`) already does dataset-scoped `Read`. A7 survives
  only as a _general_ loom deferral for large/dynamic multi-tenant RLS
  (`fut-fgac-subject-attribute`), not for Grimoire.
- **The scope axis rides the partition.** `full` / `partial` / `name_only` becomes
  _what gets written into the per-character slice_: a `full` grant writes the whole
  entity row, `partial` writes only `revealed_details`, `name_only` writes a
  recognition stub (name + type, no retrievable body). No column masking policy
  required. During play the hot tier owns the grant table and enforces this as
  entities are revealed; at check-in the grant flips materialize as writes to the
  right per-character dataset.
- **Cost:** a fact visible to a subset of players duplicates across those slices. At
  campaign volume this storage cost is negligible, and consistency is handled by the
  per-dataset versioning (§2.4). The exact global-vs-slice split per table is an
  open impl decision (§9).

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
hot tier). Status: **shipped**, queue + worker + DataFusion compute plus
Transform-output-to-Iceberg (`road-iceberg-transform-writes`, #165, ask A2). The
embedding-generation Transform itself (A3b) stays application-side: Loom stores the
vector column, an external process computes the embedding (§5).

## 5. Vectors

Three separable concerns, different homes:

1. **Store** the embedding as an Iceberg column (Arrow `FixedSizeList<Float32, N>`),
   governed and lineage-tracked like any column. This is the **truth**. Shipped:
   `road-vector-column-type` (#168, ask A3).
2. **Generate** it with an embedding **Transform** (§4): a Transform reads/writes
   the column with lineage (shipped), while the embedding call itself is
   application code (A3b, external). Every vector records its model version and
   source snapshot, governed, reproducible embeddings.
3. **Serve** ANN from the hot tier: a per-game **pgvector** index built at
   checkout (or Spanner-native vector if that tier). At a campaign's scale this
   build is trivial. New embeddings created in-game flow back via the Transform on
   check-in.

So Loom owns storage + generation + lineage. Loom has _also_ since grown an
engine-side ANN path (Puffin exact + IVF-Flat + HNSW + a `/search` kNN endpoint),
but Grimoire **serves ANN from its hot tier by choice**: the grants and the
vectors sit together there during play, so no governance seam arises (§5.1). The
loom-native serving path is now a real option should between-game direct search
ever be wanted.

### 5.1 Governance seam (only if reading vectors from Loom directly)

Inside a game this never arises: the hot tier holds the grants and the vectors
together. If a _between-game_ flow searches Loom's vectors directly, the
dataset-partition model (§3.4) already provides the seam without any FGAC: search
is scoped by dataset-level `Read`, so a subject's kNN runs over `facts_global ∪
facts_<player>` and returns only granted candidates by construction. No
over-fetch-and-drop, no per-subject row policy. (The earlier plan leaned on FGAC
here; the partition dissolves that too.)

### 5.2 Landscape (2026): why the serving index is external

- **Native Iceberg vector type/index: no.** The community proposal
  ([apache/iceberg #12636](https://github.com/apache/iceberg/issues/12636)) was
  closed as not planned; v3 added variant/geometry/geography, not vector.
- **AWS S3 Vectors: a companion store, AWS-only.** A separate vector-bucket type
  paired with S3 Tables (managed Iceberg), not a native in-Iceberg index, and not
  available on self-hosted S3 (SeaweedFS). It validates the "derived index
  materialised from the lakehouse" pattern; it is not a tool we can run.
- **Puffin-backed vector indexes: Loom shipped this.** The in-chokepoint path
  (an ANN index attached as a **Puffin sidecar bound to an Iceberg snapshot**;
  cf. [arXiv 2606.04196](https://arxiv.org/abs/2606.04196)) is what Loom's
  `road-puffin-vector-index` / `road-ivf-vector-index` / `road-hnsw-vector-index`
  now implement, exposed via the `/search` endpoint. Vector search can run in the
  governed chokepoint with dataset-level `Read` applied in the same query. Grimoire
  does not depend on it (it serves from the hot tier, §5), but the "external serving
  index" framing below is now a preference, not a necessity.

### 5.3 Optional: self-hosted S3 index object (between-game)

For between-game ANN without the hot tier, a free-standing index object on S3
works under eventual consistency: build over snapshot `S_idx`, serialize to an
immutable key, flip a one-key `CURRENT` pointer (atomic), load + cache at query
time, ANN returns entity ids, hydrate through Loom. The live read self-heals
deletions (truth is the live snapshot); only brand-new rows lag until rebuild. The
minimal metadata is that one pointer. This is now **superseded**: Loom's shipped
Puffin/`/search` path (§5.2) is the between-game index if wanted. Kept only as a
record of the dependency-free fallback; pgvector covers in-game, so this is at most
a prep-browsing convenience.

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

## 7. Loom readiness ledger

The original §7 was a Loom feature roadmap (L1-L7). Every item has resolved, so it
is now a **ledger of what Grimoire builds against**, not a wishlist. Ids are the
loom-side ask ids (A1-A7 from `docs/grimoire-kg-agenda.md`); the old L-ids are noted
for continuity.

| Ask (old id) | Capability                               | Status                                                                                                                                  |
| ------------ | ---------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| A1 (L1)      | Iceberg overwrite / replace write mode   | ✅ **shipped** `road-iceberg-overwrite-mode` (#152)                                                                                     |
| A2 (L2)      | Transform output to Iceberg              | ✅ **shipped** `road-iceberg-transform-writes` (#165)                                                                                   |
| A3 (L3)      | Vector column type                       | ✅ **shipped** `road-vector-column-type` (#168); engine-side ANN + `/search` **overshot** the ask                                       |
| A3b          | Embedding-generation Transform           | **external** (untracked): Loom stores the column, application code embeds                                                               |
| A4 (L4)      | Governed bulk read/export (Arrow Flight) | ✅ **shipped** Flight data plane (#157) + `road-governed-flight-export` (#204)                                                          |
| A5 (L5)      | Governed UPDATE / DELETE actions         | ✅ **shipped** `road-update-delete-actions` (#208)                                                                                      |
| A6 (L6)      | Per-session atomic check-in commit       | **dissolved**: versioned append-only datasets + external retry (§2.4); retired `fut-cow-session-checkin`                                |
| A7 (L7)      | Fine-grained ACL (subject-attribute RLS) | **dissolved for Grimoire**: dataset-partition + coarse per-dataset `Read` (§3.4); kept as general deferral `fut-fgac-subject-attribute` |

**Already sufficient, no ask:** recursive-CTE graph traversal, FK + join-table
(`iss-recursive-cte-iceberg`); single-hop links; atomic snapshot + lineage on the
ingest path; DataFusion-native Iceberg serving + per-column-stats predicate
pushdown (`road-iceberg-datafusion-serving`, `road-iceberg-percolumn-stats`).

**Still not building in Loom (by preference, not blocker):** an ANN engine (serve
from the hot tier; loom's own ANN now exists if wanted); a native property-graph
engine (recursive joins suffice). The one _optional_ loom nicety on the register is
`fut-ingest-overwrite-endpoint` (first-class full-state replace check-in, §2.4).

### 7.1 Grimoire implementation plan (composing shipped primitives)

With the substrate ready, the build is entirely Grimoire-side: the
checkout / play / check-in loop plus the ingest enrichment DAG. Ordered so each
step is exercisable on its own.

1. **Ontology + ingest into Loom.** Define the `ObjectType`s (§3.1), `LinkDef`s
   (§3.2), the `KnowledgeChunk` type with its vector column (§3.3, §5), and the
   dataset partitions: `global` + one `facts_<player>` per party slot (§3.4). Wire
   the extract → resolve → chunk → embed → link pipeline as a Transform chain (§4),
   run at new-book ingest. Verify: a sourcebook lands as governed, lineage-tracked
   Iceberg tables with embeddings populated.
2. **Checkout (hydrate the hot tier).** At game start, governed bulk-read the
   campaign working set (per-campaign mutable datasets + the subject's readable
   partitions) over Arrow Flight (`road-governed-flight-export`) into Postgres +
   pgvector; build the per-game ANN index (§5). Verify: checkout latency and a
   correct `global ∪ facts_<player>` view per subject.
3. **Play (hot tier owns everything).** All live reads/writes/vector/graph/fan-out
   run in the hot tier (§2.1, §3.5): HP, dice, feed, transcripts, homebrew entities,
   grant flips. Loom is untouched. Grant flips during play write to the in-tier
   grant table and mark which per-character dataset each reveal targets (§3.4).
4. **Check-in (external orchestration).** At game end, flush the session delta back
   per dataset (§2.4): append transcripts/events, apply mutable state via governed
   UPDATE/DELETE (#208), write grant reveals into the right `facts_<player>`
   dataset, each as a per-dataset Iceberg snapshot with "session N" lineage.
   Idempotent, retry-until-complete, keyed by session id; keep the hot tier until
   all datasets acknowledge, then drop + GC. Verify: kill the orchestrator
   mid-flush and confirm retry heals without corruption.
5. **Hot-tier decision.** Ship on Postgres + pgvector (§2.3). Run the Spanner Omni
   spike in parallel; it swaps only step 2/3's engine, not the SoR or check-in.

The remaining decisions inside these steps are §9.

## 8. What stays out of Loom (owned by the hot tier / external)

Live OLTP and sub-second fan-out (hot tier + WS gateway), ANN serving (pgvector /
Spanner-native by choice; loom-native available), the graph traversal engine
(recursive SQL in either tier), and per-subject grant enforcement _during play_
(the hot-tier grant table decides which `facts_<player>` dataset each reveal lands
in on check-in). Between games, dataset-level `Read` (§3.4) covers gating with no
extra machinery.

## 9. Open implementation decisions

The Loom-side questions (FGAC priority, per-session atomic commit) are closed by
the dataset-partition model. What remains is Grimoire-side and blocks §7.1, not
Loom:

- **Nested values** (§3.1): struct columns vs child ObjectTypes for
  `actions[]` / `ability_scores{}`.
- **Global-vs-slice split** (§3.4): exact per-table rule for what lands in `global`
  vs a `facts_<player>` dataset, and how `partial` / `name_only` reveals are
  represented in a slice (revealed-details row vs recognition stub).
- **Check-in write shape** (§2.4): append + governed UPDATE/DELETE over per-session
  dataset snapshots now, vs adopting the optional `fut-ingest-overwrite-endpoint`
  full-state replace when it lands. Decide whether the whole working set replaces or
  only the diff commits.
- **Hot-tier choice** (§2.3): outcome of the Spanner Omni spike vs Postgres v1.
- **Embedding model + dim** in the Transform (A3/A3b): which model, what dimension,
  and whether the vector's recorded lineage (model version + source snapshot) is
  enough to reproduce it.
