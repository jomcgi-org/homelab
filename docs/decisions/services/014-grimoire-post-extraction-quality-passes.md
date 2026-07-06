# ADR 014: Grimoire post-extraction quality passes (stat verifier, alias merge)

**Author:** Joe McGinley (with Claude Fable)
**Status:** Accepted
**Created:** 2026-07-06

---

## Problem

The v6 full-corpus extraction (40,513 chunks, 33,789 entities, 45,394 edges) ships
with strong inline guards: a deterministic relationship type-signature validator,
site-scoped location identity, and a numeric grounding gate that drops creature
ac/hp/cr and spell level values not anchored in the source chunk. The post-run gate
scan (162-item stratified sample) measured 92% of entities fully grounded, but the
residual 3% contradicted class concentrates in exactly the fields the inline gates
do not cover:

1. **Free-form detail fidelity.** Table rows and prose detail fields are not
   pattern-checkable at extraction time. Confirmed examples: a d10 damage-type
   table stored with a d8 label and three shifted rows (one damage type silently
   dropped, another duplicated), and a background whose `equipment` list was
   fabricated wholesale. Wrong-but-plausible game data is worse than missing data:
   a DM reads it at the table and has no signal that it is wrong.
2. **Split identities.** Exact-name dedup creates twin nodes when the books refer
   to one person by both a short and a full name ("Gundren" and "Gundren
   Rockseeker" are separate NPCs, each with its own mentions and edges). Search
   returns both; edges divide between them; neither node is complete.

Both defects are cheaper and safer to repair as passes over the finished graph
than to prevent inline, because the evidence needed to judge them (all of an
entity's mention chunks, all co-mentions of a candidate alias pair) only exists
after extraction completes. The graph now carries the joins these passes need:
every edge records its source `chunk_id`, every entity its mention chunks and
`source_book`, and locations/tables their scoping keys.

---

## Decision

Adopt two idempotent, re-runnable post-extraction passes, both operating on the
finished graph and both deliverable independently of any future re-extraction.

**Pass 1: evidence-grounded stat verifier.** A trailing job sends one cheap
DeepSeek call per verifiable entity: the entity's stored numeric detail, table
rows, or structured prose fields (background equipment, class feature lists),
plus its evidence (the mention chunks that contain stat-block or table markers),
with the question "is this accurate per the evidence: true/false, and if false,
the correction". The prompt states explicitly that *false with no correction is
the expected answer when the evidence contains no stat block or table*: that
distinguishes "wrong" from "unverifiable". Corrections are applied in place;
unverifiable values are nulled, consistent with the extraction-time policy of
"enrich prose, never enrich numbers". Verification with evidence is a reading
comprehension task, not a recall task, so using the same model family as the
extractor is not meaningfully circular.

**Pass 2: report-first alias merge.** A candidate generator finds same-type,
same-`source_book` entity pairs where one canonicalized name is a strict prefix
(or token-subset) of the other and at least one chunk mentions both. Candidates
are emitted as a reviewable report, not auto-applied: a human approves the list
(or a curated allow-list of patterns), and only then does the merge execute by
repointing mentions and edges to the surviving node (the longer, more specific
name wins), key-merging detail JSONB with the survivor's values taking
precedence, and deleting the twin. False merges are the corruption class the
site-scoping work exists to prevent, so this pass never merges without review.

| Aspect | Today | Decided |
| ------ | ----- | ------- |
| Numeric stats | Inline grounding gate only (ac/hp/cr/level) | Gate + trailing evidence verification with corrections |
| Table rows / prose detail | Unverified after extraction | Verified against mention-chunk evidence; wrong values corrected, unverifiable nulled |
| Name variants of one entity | Permanent twin nodes | Report-first merge into the more specific name |
| Repair trigger | Full re-extraction (~$8, hours) | Targeted re-runnable passes (pennies, minutes) |

---

## Architecture

```mermaid
graph LR
    subgraph graph [Extracted graph]
        E[entity + detail JSONB]
        M[chunk_entity_mention]
        R[relationship + chunk_id]
        K[knowledge_chunk]
    end
    E --> J1[verifier job]
    M --> J1
    K --> J1
    J1 -->|true/false + correction| DS[DeepSeek v4-flash]
    J1 -->|apply correction / null unverifiable| E
    E --> J2[alias candidate generator]
    M --> J2
    J2 -->|candidate pairs report| H[human review]
    H -->|approved pairs| J3[merge executor]
    J3 -->|repoint mentions + edges, delete twin| graph
```

Verifier scope and mechanics:

- Worklist: entities whose detail carries verifiable structure: creatures/spells
  with numeric fields, `table` entities (rows and dice), backgrounds and class
  features with equipment/list fields. Order 10-15k calls per full pass; at
  flash pricing with prompt caching this is well under a dollar.
- Evidence selection: the entity's mention chunks, filtered to those containing
  stat-block or table markers; capped per call. Entities with no marker-bearing
  evidence chunk are verdict "unverifiable" without an API call.
- A verification marker (analogous to `chunk_extraction`) keyed by
  `(entity_id, verifier_version)` makes the pass resumable and idempotent, and
  makes a re-verification after a prompt fix a deliberate version bump.
- Corrections write through the existing enrich paths so JSONB merge semantics
  stay in one place; every change is counted in a run summary (verified,
  corrected, nulled, unverifiable) like the extraction validators.

Alias merge mechanics:

- Candidate signal, all required: same `entity_type`; same `source_book`; one
  canonicalized name a strict prefix or token-subset of the other; at least one
  co-mention chunk. Locations additionally require compatible `site` values.
- The report lists each pair with its co-mention snippet so review is a scan,
  not an investigation. Approval is per-pair (or per obvious pattern).
- Merge executor is a transaction per pair: repoint `chunk_entity_mention` and
  `relationship` endpoints (dropping rows that would violate the edge unique
  triple), key-merge detail, delete the twin and its embedding, re-embed the
  survivor if its summary text changed.

## Alternatives Considered

- **Prevent inline instead (strict grounding at extraction).** Rejected for
  table rows and prose: no reliable pattern exists at chunk time, and the
  fields are exactly where model memory is plausible-but-wrong; evidence-based
  verification after the fact is strictly more accurate.
- **Local Qwen as the verifier for family independence.** Rejected for the hot
  path: the shared vLLM server runs ~8 concurrent sequences against DeepSeek's
  2,500, and evidence-grounded verification does not need family independence
  (the failure mode being checked, memory-fill, cannot recur when the judge is
  instructed to use only the supplied evidence).
- **Auto-applied alias merging.** Rejected: a false merge silently corrupts two
  entities and their edges, the same damage class the site-scoping work
  eliminated. Review cost is low (hundreds of pairs, once per corpus).
- **Embedding-similarity alias detection.** Deferred: name-prefix plus
  co-mention covers the observed defect (short vs full names) with near-zero
  false-positive risk; semantic matching ("the Glass Staff" = Iarno) is a
  later, human-heavier extension.

## Security

Baseline per `docs/security.md`. The verifier sends sourcebook chunk text to the
DeepSeek API, the same data flow as extraction itself (no new exposure). Both
passes run in the private tier with the existing `monolith-pg-app` credentials;
the merge executor writes only to grimoire derived tables and never touches
`knowledge_grant` (same invariant as the wipe script).

## Risks

| Risk | Likelihood | Impact | Mitigation |
| ---- | ---------- | ------ | ---------- |
| Verifier "corrects" a true value to a wrong one | Low | Med | Corrections only when evidence chunk contains the field's marker; run summary + spot-check sample per pass |
| Evidence chunk is itself OCR-mangled | Med | Low | Verdict unverifiable (null), never a confident wrong correction |
| Alias merge repoints an edge into a unique-triple collision | Med | Low | Drop-duplicate semantics in the executor transaction |
| Verifier version churn re-verifies the corpus needlessly | Low | Low | `(entity_id, verifier_version)` markers make cost visible and deliberate |

## Open Questions

1. Whether table-row corrections should rewrite rows in place or store a
   `verified_rows` sibling key (in-place favored: one source of truth).
2. Whether the alias report should land as a PR artifact (reviewable diff) or a
   monolith UI queue; PR artifact favored for provenance.

## References

| Resource | Relevance |
| -------- | --------- |
| [ADR 012](012-grimoire-postgres-first-loom-shaped.md) | Dedup + enrich semantics the passes build on |
| [ADR 011](011-grimoire-hot-tier-schema.md) | Entity spine / typed detail model |
| `projects/monolith/grimoire/prompt-backlog.md` | Extraction-side defect evidence (class_feature recall, tables) |
| PR #3249, #3252 | Inline guards these passes complement (numeric gate, site scoping, provenance, table type) |
