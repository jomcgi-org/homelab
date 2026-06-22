# Grimoire / KG on Loom: ontology mapping and gap analysis

> **Status:** decision input, not a decision. This maps Grimoire's data model
> (`data-architecture.md`) and a personal knowledge graph onto Loom's current
> primitives, then lists the exact gaps. It feeds a downstream choice between
> shipping the KG on Postgres now vs. driving Loom's roadmap to host it.
>
> Loom state of record: pre-alpha, HEAD `cb70424` (2026-06-22). Primitives cited
> from `src/control-plane/core/src/{ontology,acl}.rs` in the loom repo.

## 1. The question

Grimoire's knowledge layer and a personal KG are both "typed objects + links +
governed visibility + provenance + vector retrieval." That is the Foundry shape
Loom is built to be. This doc checks whether the _current_ Loom primitives can
express that model, primitive by primitive, and names what is missing.

## 2. Loom primitives (what we are mapping onto)

| Primitive            | Shape                                                                                                                          | Source            |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------ | ----------------- |
| `ObjectType`         | name + ordered typed `PropertyDef`s + `derived` (aggregate-over-link) + backing `TableRef` + optional `identity` (PK property) | `ontology.rs:32`  |
| `PropertyDef`        | `{name, ty (logical type string), required}` bound to a physical DuckLake column                                               | `ontology.rs:24`  |
| `LinkDef`            | directed `from -> to`, `Cardinality::{One,Many}`, `LinkBacking::{ForeignKey, JoinTable}`, reversible                           | `ontology.rs:104` |
| `DerivedPropertyDef` | computed `Count/Sum/Avg/Min/Max` over **one** named link                                                                       | `ontology.rs:127` |
| `ActionDef`          | named write. **Part-1 semantics: insert ONE new instance of `target`.** No update/delete.                                      | `ontology.rs:149` |
| `Policy`             | per `(role, target=Type` or `Table)`: `row_filter` + `deny_columns` + `mask_columns`                                           | `acl.rs:106`      |
| `RowFilter`          | boolean tree of `Compare{property, op, value}` over **literal** `ScalarValue::{Text,Int,Bool,List}`                            | `acl.rs:90`       |
| `Acl::check`         | coarse `(subject, Read` or `Write, target)` grant, Deny-wins                                                                   | `acl.rs`          |
| Lineage              | OpenLineage event emitted atomically with each snapshot commit                                                                 | `ARCHITECTURE.md` |

Two hard constraints fall straight out of these types:

- **Properties are typed columns, not documents.** There is no jsonb/map logical
  type and no way to filter or mask _inside_ a nested value. The ontology is
  columnar by construction (it resolves to a DuckLake table).
- **Writes are append/insert-shaped.** Bulk ingest commits snapshots; actions
  (when built) insert one instance. There is no governed UPDATE or DELETE path.

## 3. Entity -> ObjectType

Grimoire's `Entity` is a **polymorphic table** with a `properties` jsonb column,
chosen explicitly to avoid "15 join tables" (`data-architecture.md` Design
Decisions). Loom pulls the opposite way: one typed, columnar `ObjectType` per
shape.

The idiomatic Loom mapping is **one `ObjectType` per `entity_type`**, each backed
by its own DuckLake table with typed columns shredded from the jsonb:

| Grimoire entity_type | Loom `ObjectType` | Representative typed properties (from the jsonb examples)                                                                                                           |
| -------------------- | ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Creature             | `Creature`        | `armor_class:Int, hit_points:Int, challenge_rating:Double, size:Text, creature_type:Text, ...` (nested `ability_scores`, `actions[]` flatten or become child types) |
| Spell                | `Spell`           | `level:Int, school:Text, casting_time:Text, concentration:Bool, ritual:Bool, effect_text:Text`                                                                      |
| Location             | `Location`        | `location_type:Text, population:Int, government:Text`                                                                                                               |
| NPC                  | `NPC`             | `role:Text, alignment:Text, race:Text, occupation:Text`                                                                                                             |
| Faction              | `Faction`         | `faction_type:Text, alignment:Text, influence_level:Text`                                                                                                           |
| Deity                | `Deity`           | `alignment:Text, province:Text, pantheon:Text`                                                                                                                      |
| Item                 | `Item`            | `item_type:Text, rarity:Text, requires_attunement:Bool`                                                                                                             |

Shared columns (`name`, `aliases[]`, `description`, `embedding_text`,
`source_type`, `source_refs[]`) repeat on each type, or you model a base
`Entity` type and link the specializations.

**Trade-off.** This is _better_ modeling than the jsonb blob (real types, real
column policy, real stats), but it costs the two things the jsonb design was
chosen for:

1. **Homebrew / unknown shapes.** A DM-authored entity with novel fields has no
   column to land in. Polymorphic jsonb absorbs that for free; typed ObjectTypes
   require a schema change. Loom has `define_type` upsert, so it is _possible_,
   but it is a control-plane write per new shape, not a row insert.
2. **Ingest cost.** The PDF pipeline must shred Flash's structured extraction
   into per-type columnar Parquet, not write one jsonb row. This is squarely
   Loom's DataFusion ingest path (a strength), but it is more pipeline than
   "insert a row."

Nested arrays (`Creature.actions[]`, `traits[]`, `ability_scores{}`) have no
clean columnar home and would become either struct columns (DuckDB supports
them; Loom's logical vocabulary does not document them) or child ObjectTypes
linked back. Open modeling question.

## 4. Relationship -> LinkDef

This is the **cleanest** mapping. Grimoire's `Relationship` table is exactly an
edge list, and every `rel_type` is a `LinkDef`.

| Grimoire Relationship                                         | Loom `LinkDef`                                                       |
| ------------------------------------------------------------- | -------------------------------------------------------------------- |
| `LOCATED_IN` (NPC/Creature/Item -> Location)                  | `LinkDef{from, to: Location, cardinality: Many, backing: JoinTable}` |
| `CONTAINS` (Location -> Location)                             | self-link, `JoinTable`                                               |
| `MEMBER_OF` (NPC -> Faction)                                  | `JoinTable`                                                          |
| `HOSTILE_TO` / `ALLIED_WITH` (Faction <-> Faction, symmetric) | `JoinTable` + `reversed()` for the inverse direction                 |
| `APPEARS_IN` (Entity -> SourceBook)                           | `JoinTable`                                                          |

Because Grimoire's `Relationship` is a single generic edge table keyed by
`rel_type`, the natural backing is **`LinkBacking::JoinTable`** for all of them
(one mapping table, `rel_type` selecting which `LinkDef` is in play), rather than
per-type FK columns. Symmetric relations use `reversed()`.

**What is built vs. needed:** single-hop governed traversal across FK and
join-table links **is built**. Grimoire's Pipeline 5 wants **1-2 hop** graph
traversal ("Entity -> Relationships -> Connected entities"). The second hop is in
Loom's "richer reads / multi-hop pending" bucket. `links()` / `links_to()` give
adjacency, so a 2-hop is expressible as two governed calls today, but not as one
compiled query.

## 5. KnowledgeChunk + ChunkEntityMention -> types + links

Straightforward structurally:

- `KnowledgeChunk` -> `ObjectType{content_text, embedding_text, section_path, ...}`
  backed by a chunks table. **Except** the `embedding` column (see gap 1).
- `ChunkEntityMention` -> a `JoinTable` link `Chunk <-> Entity` carrying
  `mention_span`, `context` as link attributes (Loom links do not currently
  carry properties, so the attributes would live on a backing mention table read
  as its own type).

The **grant-ratio** and **first-mention-drop** chunk-filtering logic
(`data-architecture.md` Pipeline 5) is application logic that sits _above_ any
datastore. It needs: per-chunk mentioned-entity set, the subject's grant set, and
a ratio computation. Loom can serve the inputs (chunk -> mentions, governed
entity reads); the policy itself is not an ACL primitive and stays in the app.

## 6. KnowledgeGrant -> ACL (the load-bearing mismatch)

This is where the model genuinely does **not** fit, and it is worth being precise
about why.

Grimoire's `KnowledgeGrant` is **per `(entity_id, player_character_id)`** with a
`grant_scope` of `full | partial | name_only` and a lifecycle
`pending -> confirmed`. It is _data_: thousands of rows, created and flipped at
session time by event detection and DM approval.

Loom's `Policy` is **per `(role, target-type)`**, static, with a `row_filter`
made of **literal** `ScalarValue`s (`acl.rs:90`). It answers "for this role, which
rows of this type, with which columns denied/masked."

The two axes of a grant map differently:

| Grant axis                                                           | Loom mechanism                                                                                                                                                                      | Fit                    |
| -------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------- |
| **scope** (`full`/`partial`/`name_only`) -> which fields are visible | three roles, each a `Policy` with different `deny_columns`/`mask_columns` (e.g. `name_only` denies everything but `name`+`entity_type`; `partial` masks all but `revealed_details`) | ✅ clean               |
| **which entities** are granted to **which player**                   | would require `row_filter` = "entity_id IN (this subject's granted set)"                                                                                                            | ❌ **not expressible** |

The blocker: a `RowFilter::Compare` value is a literal `List`, not a subquery or
a subject-attribute reference. To gate "the entities _this player_ has been
granted," the filter must join `KnowledgeGrant` on the current subject. Loom's
ACL explicitly never resolves or joins beyond the literal tree (`acl.rs:8`,
"the control plane never interprets a filter"). So per-instance, per-subject,
dynamic grants are a structurally different thing from a Loom Policy.

Workarounds, all imperfect:

- **Recompile policy on every grant change.** Treat each player as a role and
  rewrite their `row_filter` `In(list)` whenever a grant flips. Correct, but it
  turns a row insert into a policy recompile, at session cadence, and the list
  grows unbounded. Abuse of the static-policy model.
- **Filter above Loom.** Keep `KnowledgeGrant` in operational Postgres, read the
  granted-id set there, and post-filter Loom results. Then Loom is not enforcing
  the grant, which defeats "governance follows the data" for the one policy that
  matters most here.

Neither is satisfying. The grant model wants **attribute-based, data-driven row
policy** (a filter referencing the subject and a join), which Loom does not have.

This is not a Grimoire quirk. It is **fine-grained access control (FGAC)**, the
mainstream enterprise governance pattern, and Loom missing it is a gap on its own
"open-source Foundry" thesis, not an impedance mismatch with this app. See
[Enterprise framing](#65-enterprise-framing-this-is-fgac-not-a-grimoire-quirk).

### 6.5 Enterprise framing: this is FGAC, not a Grimoire quirk

"A subject sees a row because of an attribute it carries, or a grant/relationship
row that links them to it" is the definition of fine-grained access control. It is
the same shape as the most common enterprise row-level-security (RLS) rules:

- **Multi-tenancy:** `row.tenant_id == subject.tenant` — the single most common
  RLS rule in SaaS.
- **Need-to-know / ownership:** visible if you are the owner, on the row's ACL, or
  your org-unit matches.
- **Entitlement tables:** healthcare care-relationship rows, deal-team membership,
  data-sharing grants. `KnowledgeGrant` is exactly this, scaled down.

Every serious governance system expresses these: Postgres RLS
(`current_setting`-parameterized policies), Snowflake row-access policies,
BigQuery row-level security, Databricks Unity Catalog, Immuta/Privacera. And it is
core Foundry: static **Markings** are only half the model; Foundry also has
object-level / data-derived security. A platform that does only static markings +
literal row filters has implemented the _easy_ half of governance.

The fix is **not** policy-as-data (millions of policies) nor app-side filtering.
It is **one policy rule that is a function of the querying principal**, closed by
two `RowFilter` extensions:

1. **Subject-attribute references** — a `ScalarValue::SubjectAttr("tenant")` so
   `region == $subject.region` compiles. Cheap; handles multi-tenancy / ABAC
   outright.
2. **An entitlement-join / `Exists` leaf** — "row's id appears in `grants` for
   this subject." One rule, data-driven membership. This is the `KnowledgeGrant`
   case.

The Query API already holds the authenticated subject (it authorizes the request),
so threading subject context into filter compilation is natural. The constraint to
relax is the ACL crate's "control plane never interprets a filter" stance
(`acl.rs:8`). Loom's ACL is explicitly an early slice (P4: "stores and serves
policy; does NOT enforce"), so the literal-only `RowFilter` reads as a deliberate
v1 floor, not a design dead-end. FGAC is the inevitable next layer.

## 7. Operational tables -> stay in Postgres

These are OLTP + realtime by shape and do **not** belong in Loom at all:

| Table                                                   | Why not Loom                                                                 |
| ------------------------------------------------------- | ---------------------------------------------------------------------------- |
| `Session`, `SessionEvent` (`pending`/`confirmed` flips) | row-level UPDATE; no Loom update path                                        |
| `SessionTranscript` (append per utterance, live)        | high-rate append + realtime fan-out; snapshot-per-row is wrong               |
| Live character state (HP/conditions), dice, feed        | <200-300ms OLTP + push to clients (Firestore listeners / WS-gateway + Redis) |
| `QueryLog`                                              | append-only audit; fine in Postgres, no governance need                      |

The split is the same one the existing `architecture.md` already draws: hot state
in Postgres/Redis/WS, knowledge in the graph store.

## 8. Embeddings -> the vector gap

`Entity.embedding`, `KnowledgeChunk.embedding`, `SessionTranscript.embedding` are
all 3072-dim vectors with HNSW cosine search and metadata filters. Loom has **no
vector property type and no ANN index**; vector search is out of scope
(`README.md`). This is the single hardest miss, because vector retrieval is the
_entry point_ of Pipeline 5 (parallel vector search + graph traversal), not a
peripheral feature.

DuckDB's VSS extension exists, but an HNSW index over object-storage Parquet
snapshots is not a live-RAG substrate, and Loom does not wire it.

## 9. Provenance -> lineage (a genuine win)

`Entity.source_refs` ([{book_id, page}]), `Relationship.source_book_id`, and
"which session revealed this lore" are exactly OpenLineage territory. Loom emits
lineage atomically with each snapshot commit. For the _ingested_ corpus this is
strictly better than the hand-rolled `source_refs[]` array: the provenance of
every entity and edge is recorded at commit time, queryable, and tamper-resistant.
This is the clearest place Loom adds value over the Postgres design.

## 10. Exact gap list (prioritized)

Each gap is tagged **[gov]** if it is a general enterprise-governance capability
Loom needs for its own Foundry thesis, or **[app]** if it is a retrieval/app need
Loom deliberately scopes out.

| #   | Gap                                                                               | Class | Loom today                                | Needed for                                    | Size                                       |
| --- | --------------------------------------------------------------------------------- | ----- | ----------------------------------------- | --------------------------------------------- | ------------------------------------------ |
| 1   | **Vector property + ANN search** with metadata filters                            | [app] | absent, out of scope (AIP-adjacent)       | Pipeline 5 entry; all RAG                     | Large (new capability)                     |
| 2   | **Fine-grained access control** (subject-attribute + entitlement-join row policy) | [gov] | `RowFilter` is literal-only               | `KnowledgeGrant`; multi-tenancy; any RLS      | Large (ACL model change, but standard)     |
| 3   | **Governed UPDATE / DELETE actions**                                              | [gov] | `ActionDef` is insert-one-only            | grant flips, homebrew edits, any mutation     | Medium-Large (open transactional question) |
| 4   | **External client wire** (Quack server or committed HTTP write API)               | [gov] | serving embedded in-process               | any client (Grimoire Go, monolith) using Loom | Medium (on roadmap)                        |
| 5   | **2-hop / chained traversal in one query**                                        | [app] | single-hop built; adjacency via `links()` | Pipeline 5 graph traversal                    | Medium (on roadmap)                        |
| 6   | **Document / nested-value properties** (struct, jsonb-ish)                        | [app] | typed scalar columns only                 | homebrew + nested stat blocks                 | Medium (or accept per-type schemas)        |

The reframe: gaps **2 and 3 are [gov]** — fine-grained access control and governed
mutation are table-stakes for any "governance follows the data" platform, not
Grimoire-specific. Foundry has both; Loom has neither yet. Gap 4 is also [gov]
(governance is moot if no external client can reach the front door) and is already
on the roadmap. Gaps 1, 5, 6 are [app] — vector and nested documents are
deliberately out of scope, multi-hop is a planned read enrichment.

So the headline is not "Loom is missing Grimoire features." It is **"Loom is
missing the harder half of enterprise governance (FGAC + governed write-back),
which it needs regardless, and Grimoire happens to exercise exactly that half."**

## 11. Read of the result

What maps cleanly: **Relationships -> Links** (5.x), **grant scope -> column
policy** (6), **provenance -> lineage** (9), **corpus bulk-load -> DataFusion
ingest** (3). What does not: **per-player grants -> row policy** (the central
governance feature), **vectors** (the retrieval entry point), and **any
mutation** (insert-only actions).

So Loom can express the _static, ingested, analytical_ half of the knowledge
graph well, and the _dynamic, per-subject, mutable, vector-retrieved_ half not at
all today. Grimoire's KG is mostly the second half. A personal KG leans the same
way (pgvector RAG + frequent note edits + private/public gating already work in
the monolith).

Crucially, the missing half is not "app features." Two of its three pillars
(per-subject access, governed mutation) are **enterprise governance** Loom owes
its own thesis; only vector retrieval is genuinely an out-of-scope app concern.
That is what reframes the A/B decision below.

## 12. Recommendation feeding the A/B choice

- The Postgres + pgvector design in `data-architecture.md` is the right substrate
  **now**: it already does vectors, per-row grants, mutations, and 1-2 hop JOINs,
  which is the exact set Loom is missing.
- Loom's defensible role is the **governed analytical/provenance tier over the
  ingested corpus** (entities, relationships, chunks, lineage), reachable once
  gaps 2, 3, 4 land. Correcting the earlier draft of this doc: the per-subject
  access gate (gap 2) and governed mutation (gap 3) are **not** "never Loom's
  job." They are FGAC and write-back, the harder half of the governance Loom
  exists to provide. They belong on Loom's roadmap on their own merit; Grimoire
  just exercises them early.
- So "make Grimoire drive Loom" is a reasonable forcing function **provided gap 2
  is built for the general FGAC case** (subject-attribute references +
  entitlement-join row policy), not a bespoke `KnowledgeGrant` feature. Built
  generally, it pays back across multi-tenancy, need-to-know, and any future
  tenant of the platform; Grimoire becomes the proof-of-use, not the design
  driver. The [app] gaps (1 vector, 6 nested docs) stay out of scope; keep those
  in the operational store.

Concrete next step if continuing: turn gaps 2, 3, 4 into Loom roadmap items framed
as **enterprise governance**, not Grimoire support —
(2) FGAC: `RowFilter` gains subject-attribute references and an entitlement-join /
`Exists` leaf, compiled by the Query API which already holds the subject;
(3) governed UPDATE/DELETE actions with a settled transactional model (the open
question in `ARCHITECTURE.md`);
(4) the external client wire (already roadmapped).
Then decide whether the lore corpus is a strong enough forcing function to pull
these ahead of Loom's current ingest/query hardening. Gap 1 (vector) stays an
[app] concern unless Loom deliberately revisits its AIP-out-of-scope line.
