# ADR 008: Monolith Module Boundaries

**Author:** Joe McGinley
**Status:** Accepted
**Created:** 2026-06-16
**Relates to:** [ADR 004 (security): Public Read-Only Service Isolation](../security/004-public-read-only-service-isolation.md)

---

## Problem

The monolith is organised into domains (`ships`, `stars`, `chat`, `knowledge`, `hikes`, `dr_jobs`, `trips`, `home`, `scheduler`, `agent`), each owning a Postgres schema. But the boundaries between domains exist only by convention. Two failure modes follow:

1. **Domains reach into each other's internals.** `chat` imports `knowledge.store.KnowledgeStore` directly; `agent` imports `chat.bot.send_message`; `agent` reads the scheduler's private `_registry`. Each is a coupling that no tool prevents from multiplying.
2. **`shared/` is a grab-bag.** It mixes a genuine domain (`scheduler`, which has its own `scheduler/` package yet whose code lives in `shared/scheduler.py`) with domain-agnostic infra (`embedding`, `forecast_freshness`, test markers) and single-consumer utilities (`chunker`, used only by knowledge; the Kubernetes client, used only by home). "Shared" conveys no ownership, so nothing stops any module depending on any other.

Without an enforceable boundary, there is no unit to scope a database role to. Per-domain DB permissions (ADR 004's `public_reader` and the read replica work) presuppose that "what a domain owns" is structurally legible. It is not, today.

## Decision

**`<domain>/api.py` is the sole cross-domain entrypoint.** A domain may import another domain only through that domain's `api.py`; it may not import another domain's internal modules, and it may not read another schema's tables. A domain's own internals remain freely importable within the domain.

**`shared/` means domain-agnostic infrastructure, importable anywhere.** Code that belongs to a domain leaves `shared/`: the scheduler moves into the `scheduler/` domain it already nominally owns; single-consumer utilities fold into their one consumer. What remains in `shared/` is genuinely cross-cutting and owned by no domain (`embedding`, `forecast_freshness`, test infrastructure).

**Genuinely-shared infrastructure keeps its shared tables and schemas.** This boundary is about import seams, not storage. The scheduler keeps its single `scheduler.scheduled_jobs` registry and its `SELECT ... FOR UPDATE SKIP LOCKED` multi-pod claim path; splitting it into per-domain tables would replace one proven tick loop with many for no benefit. The seam we draw is at the function (`api.py`), not the table.

**The boundary is enforced in CI.** An import-graph test parses every domain module and fails the build on any cross-domain import that does not go through `<domain>.api`. The boundary is a property the build checks, not a convention that drifts.

## Alternatives Considered

- **Semgrep per-domain rules.** Rejected: the rule is relational (it depends on both the importing file's domain and the imported module's domain), which semgrep's single-file pattern model expresses poorly. An AST import-graph test states the rule once and reads the whole tree.
- **Convention only, documented but unenforced.** Rejected: the three existing violations show convention alone does not hold the line. The cost of the guard is one small test.
- **Per-domain database tables for shared infrastructure (e.g. a scheduler table per domain).** Rejected: it breaks the single-registry concurrency model and adds pollers for no gain. Shared infra stays shared at the storage layer; only the import seam is per-domain.

## Consequences

- Per-domain DB role scoping (ADR 004) now has a legible unit to attach grants to: the schema a domain owns, reached in code only through its `api.py`.
- New cross-domain needs must be expressed as an explicit `api.py` addition, which is visible in review rather than buried in an internal import.
- `shared/` shrinks to infra and gains a clear meaning, so "is this shared?" stops being a judgement call.
