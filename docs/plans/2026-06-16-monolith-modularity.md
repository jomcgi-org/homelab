# Monolith Modularity Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Turn the monolith's implicit domain structure into enforced module boundaries, then layer database-permission and read-replica isolation on top, culminating in a separate read-only public service (ADR 004).

**Architecture:** Domains keep their schema-per-domain Postgres layout inside the single `monolith` database. Cross-domain coupling moves behind an explicit `<domain>/api.py` seam enforced by an import-graph guard. Genuinely-shared infra (scheduler, embedding) keeps its shared tables/schemas; only the _functions_ relocate to an owning module. On that foundation: a `public_reader` role + public views (confidentiality), a CNPG read replica (load/availability), a ClickHouse-decoupling rollup, and a separately-composed public service that holds no secrets.

**Tech Stack:** Python 3 / FastAPI / SQLModel / psycopg3, CloudNativePG, Atlas migrations, semgrep + pytest (BuildBuddy CI), apko images, SvelteKit frontend.

---

## Repo workflow constraints (read before executing)

- **No local test loop.** Do NOT run `pytest` / `bazel test` locally. Implement, commit with Conventional Commits, push the branch, watch CI with `gh pr checks <n> --watch`. Per CLAUDE.md, defer all test execution to end-of-phase CI on the pushed branch. The "verify it fails / passes" steps below describe the _intended_ CI outcome, not a local run.
- **One PR per phase; one comprehensive review per merged PR.** Each phase is a separate worktree + PR. Do not open per-task reviews.
- **Never commit to main.** Already in worktree `/tmp/claude-worktrees/monolith-modularity` on `feat/monolith-modularity`.
- **`# gazelle:exclude knowledge`**: any new file under `knowledge/` (and any new test target) must be hand-registered in `projects/monolith/BUILD`. Check that BUILD after adding `knowledge/api.py` and `knowledge/chunker.py`.
- **No em-dashes** in any code, comment, commit, or doc.
- **Chart version bumps** (Phases 3+): bump `chart/Chart.yaml` AND `deploy/application.yaml` `targetRevision` together.

---

## Program Overview

Five phases, executed in dependency order. Each lands and passes CI before the next begins.

```
Phase 1  Module boundaries        (items 1+2)  ── refactor only, no schema change
   │                                              enables ↓
Phase 2  Roles + public views     (item 3)     ── primary migration, additive
   │                                              consumed by ↓
Phase 3  Read replica             (item 4)     ── CNPG instances:2 + -ro service
   │
Phase 4  ClickHouse decouple                   ── rollup job → observability.*_snapshot
   │
Phase 5  Public service split     (ADR 004 L1) ── separate artifact, public_reader on -ro
```

**Why this order:** boundaries (1) must exist before a role can be scoped to "what a domain owns" (2/3). The replica (3) is independent infra that the public reader (5) consumes. ClickHouse decoupling (4) removes the last non-Postgres dependency from the public hot path so the split service's entire dependency set is the replica.

**Rationale of record:** ADR 004 (`docs/decisions/security/004-public-read-only-service-isolation.md`) already decides Phases 2-5's _security_ rationale. Phase 1 (module boundaries) is a new architectural decision and gets its own short ADR (Task 1.0). When Phase 5 lands, flip ADR 004 status Draft -> Accepted.

**Detail level:** Phase 1 is fully task-broken-down below. Phases 2-5 are specified at file + approach + key-task level and reference ADR 004; expand each into bite-sized steps at the start of its own PR (some specifics, e.g. replica node sizing, depend on Phase 1-3 runtime outcomes).

---

# PHASE 1: Module boundaries (this PR)

**Outcome:** Every cross-domain call goes through `<domain>/api.py`; an import-graph guard fails CI on violations; `shared/` holds only domain-agnostic infra. Pure refactor: no schema, endpoint, or behavior change. Existing tests are the safety net.

**Current cross-domain coupling (the complete set to fix):**

- `chat/router.py` -> `from knowledge import get_store`
- `chat/explorer.py` -> `from knowledge.store import KnowledgeStore` (reaches past the seam)
- `agent/notify.py` -> `from chat.bot import send_message`
- `agent/checks.py` -> `from shared.scheduler import _registry` (private symbol)
- `scheduler/service.py` -> `from shared.scheduler import ScheduledJob, _registry`
- ~7 domains -> `from shared.scheduler import register_job`

**`shared/` disposition:**
| Module | Consumers | Destination |
| --- | --- | --- |
| `scheduler.py` | 7 domains + app + agent | -> `scheduler/api.py` (scheduler domain owns it) |
| `chunker.py` | knowledge only | -> `knowledge/chunker.py` (internal) |
| `kubernetes.py` | home only | -> `home/observability/kubernetes.py` (internal) |
| `embedding.py` | chat + knowledge | stays `shared/` (infra primitive) |
| `forecast_freshness.py` | stars + hikes | stays `shared/` (infra primitive) |
| `testing/` | all test targets | stays `shared/` (test infra) |

After Phase 1, `shared/` means "domain-agnostic infra, importable anywhere." The guard encodes that.

---

### Task 1.0: Record the module-boundary decision (ADR)

**Files:**

- Create: `docs/decisions/platform/00X-monolith-module-boundaries.md` (next free number in `platform/`, run `ls docs/decisions/platform/` to pick it)

**Step 1:** Write a short ADR (rationale only, no phase checklists per the repo's ADR convention): Problem = domains coupled by direct imports + a `shared/` grab-bag, so there is no enforceable boundary to scope DB roles to. Decision = `<domain>/api.py` is the sole cross-domain seam; `shared/` is domain-agnostic infra; an import-graph guard enforces it in CI. Alternatives = semgrep per-domain rules (rejected: relational rule, awkward in semgrep), convention-only (rejected: drifts). Reference ADR 004 as the consumer of this boundary.

**Step 2: Commit**

```bash
git add docs/decisions/platform/00X-monolith-module-boundaries.md
git commit -m "docs(adr): record monolith module-boundary decision"
```

---

### Task 1.1: Relocate the scheduler into the scheduler domain

The scheduler code currently lives in `shared/scheduler.py` but the `scheduler/` domain already exists and re-imports it. Move the code to where the domain is, and add a public registry accessor so no caller needs the private `_registry`.

**Files:**

- Create: `projects/monolith/scheduler/api.py` (verbatim move of `shared/scheduler.py` content)
- Create: `projects/monolith/scheduler/api_test.py` ... move the 7 `shared/scheduler_*_test.py` files into `scheduler/` (rename imports `shared.scheduler` -> `scheduler.api`)
- Delete: `projects/monolith/shared/scheduler.py` and the 7 `shared/scheduler_*_test.py` files
- Modify: `projects/monolith/scheduler/service.py:10`, `projects/monolith/agent/checks.py:75`, and every `from shared.scheduler import ...` site (see import map)
- Modify: `projects/monolith/BUILD` (re-register moved test targets; scheduler is not gazelle-excluded but verify)

**Step 1:** `git mv shared/scheduler.py scheduler/api.py`. Update its module docstring to "Scheduler domain API: Postgres-backed job scheduler with distributed locking."

**Step 2:** Add a public accessor to `scheduler/api.py` so callers stop touching `_registry`:

```python
def is_registered(name: str) -> bool:
    """True if a handler is registered for ``name`` (public view of the registry)."""
    return name in _registry


def registered_names() -> list[str]:
    """Names of all jobs with a registered handler."""
    return list(_registry)
```

**Step 3:** `git mv` the 7 `shared/scheduler_*_test.py` files into `scheduler/`, and rewrite their `from shared.scheduler import ...` to `from scheduler.api import ...`.

**Step 4:** Update every non-test import site to `from scheduler.api import ...`:

- `scheduler/service.py:10` -> `from scheduler.api import ScheduledJob, is_registered` (replace `_registry`/`_to_view`'s `job.name in _registry` with `is_registered(job.name)`)
- `agent/checks.py:75` -> `from scheduler.api import is_registered` (replace `_registry` usage)
- `app/main.py:59` -> `from scheduler.api import purge_stale_jobs, run_scheduler_loop`
- `dr_jobs/__init__.py`, `hikes/__init__.py`, `home/__init__.py`, `ships/__init__.py`, `stars/__init__.py`, `chat/summarizer.py`, `knowledge/service.py` -> `from scheduler.api import register_job`
- Test files referencing `shared.scheduler` (`chat/startup_test.py`, `chat/summarizer_startup_test.py`, `knowledge/service_test.py`, `agent/tests/bdd_checks_test.py`) -> `from scheduler.api import ...` (use `is_registered`/`registered_names` instead of `_registry` where possible; if a test needs to mutate the registry, import `scheduler.api` module and patch its `_registry` attribute explicitly).

**Step 5:** Update `projects/monolith/BUILD` to register the moved `scheduler/*_test.py` targets and drop the `shared/scheduler*` ones.

**Step 6: Commit**

```bash
git add -A
git commit -m "refactor(monolith): move scheduler from shared/ into the scheduler domain"
```

---

### Task 1.2: Fold single-consumer utils into their owning domain

**Files:**

- `git mv shared/chunker.py knowledge/chunker.py` (+ its 3 `chunker_*_test.py` -> `knowledge/`)
- `git mv shared/kubernetes.py home/observability/kubernetes.py` (+ `kubernetes_test.py` -> `home/observability/`)
- Modify: `knowledge/indexing.py:24`, `knowledge/store.py:16` (`from knowledge.chunker import ...`)
- Modify: `home/observability/stats.py:28` (`from home.observability.kubernetes import KubernetesClient`)
- Modify: `projects/monolith/BUILD` (knowledge is `gazelle:exclude` -> hand-register `knowledge/chunker.py` + moved tests; re-register home test target)

**Step 1:** `git mv` the chunker files into `knowledge/`, rewrite imports.

**Step 2:** `git mv` the kubernetes files into `home/observability/`, rewrite imports.

**Step 3:** Hand-register in `projects/monolith/BUILD`: `knowledge/chunker.py` as a `py_library` source (or fold into the knowledge library glob) and `knowledge/chunker_*_test.py` targets; update home targets.

**Step 4: Commit**

```bash
git add -A
git commit -m "refactor(monolith): fold chunker into knowledge and k8s client into home/observability"
```

---

### Task 1.3: Establish the `knowledge/api.py` seam

**Files:**

- Create: `projects/monolith/knowledge/api.py`
- Modify: `projects/monolith/knowledge/__init__.py` (keep only `register`; re-export api for back-compat optional)
- Modify: `projects/monolith/chat/router.py:13`, `projects/monolith/chat/explorer.py:11,31`
- Modify: `projects/monolith/BUILD` (hand-register `knowledge/api.py`)

**Step 1:** Create `knowledge/api.py` as the public seam. Move `search_notes`, `get_store`, `get_embedding_client` out of `__init__.py`, and re-export `KnowledgeStore` for type annotations so consumers never import `knowledge.store` directly:

```python
"""Knowledge domain public API: the only surface other domains may import."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlmodel import Session

from knowledge.store import KnowledgeStore  # re-exported for typing


def get_store(session: "Session") -> "KnowledgeStore":
    return KnowledgeStore(session)


def search_notes(session: "Session", query_embedding: list[float], **kwargs):
    return KnowledgeStore(session).search_notes_with_context(
        query_embedding=query_embedding, **kwargs
    )


def get_embedding_client():
    from shared.embedding import EmbeddingClient

    return EmbeddingClient()
```

**Step 2:** Trim `knowledge/__init__.py` to just `register(app)` (domain wiring used only by `app/main.py`). Optionally re-export from api for any internal callers, but external callers must use `knowledge.api`.

**Step 3:** Update chat:

- `chat/router.py:13` -> `from knowledge.api import get_store`
- `chat/explorer.py:11` -> `from knowledge.api import KnowledgeStore` (annotation only)

**Step 4:** Register `knowledge/api.py` in `projects/monolith/BUILD` (gazelle-excluded dir).

**Step 5: Commit**

```bash
git add -A
git commit -m "refactor(monolith): expose knowledge cross-domain surface via knowledge/api.py"
```

---

### Task 1.4: Establish the `chat/api.py` seam

**Files:**

- Create: `projects/monolith/chat/api.py`
- Modify: `projects/monolith/agent/notify.py:12`
- Modify: `projects/monolith/BUILD` if chat needs a new registered source

**Step 1:** Create `chat/api.py`:

```python
"""Chat domain public API: the only surface other domains may import."""

from __future__ import annotations

from chat.bot import send_message  # re-exported

__all__ = ["send_message"]
```

**Step 2:** `agent/notify.py:12` -> `from chat.api import send_message`.

**Step 3: Commit**

```bash
git add -A
git commit -m "refactor(monolith): expose chat send_message via chat/api.py"
```

---

### Task 1.5: Add the import-boundary guard (the enforcement)

**Open implementation choice (recommended: AST test).** Enforce with a pytest that parses every domain `.py` with the `ast` module, builds the import graph, and asserts the rule. This is preferred over a semgrep rule because the rule is relational and path-aware (file's domain vs imported domain), which semgrep handles poorly. If you instead want it in the semgrep suite, that is the alternative; the assertion logic is identical.

**Rule:** For module file under `projects/monolith/<domain>/` where `<domain>` is in the domain set, any `import <other_domain>...` or `from <other_domain>... import ...` is a violation UNLESS the imported module is exactly `<other_domain>.api` (or a submodule explicitly allow-listed). `shared`, `app`, and `infra-agnostic` packages are importable anywhere. A domain importing its own internals is fine.

**Files:**

- Create: `projects/monolith/import_boundaries_test.py`
- Modify: `projects/monolith/BUILD` (register the test target)

**Step 1: Write the guard test (it should PASS once Tasks 1.1-1.4 landed):**

```python
"""Guard: domains may only import each other via <domain>.api.

See docs/decisions/platform/00X-monolith-module-boundaries.md.
"""

import ast
import pathlib

DOMAINS = {
    "ships", "stars", "chat", "knowledge", "hikes",
    "dr_jobs", "trips", "home", "scheduler", "agent",
}
# Imports of another domain that are allowed without going through .api.
# Keep this empty; entries here are documented exceptions, not the norm.
ALLOW: set[tuple[str, str]] = set()  # (importing_domain, imported_module)

ROOT = pathlib.Path(__file__).parent


def _domain_of(path: pathlib.Path) -> str | None:
    rel = path.relative_to(ROOT)
    top = rel.parts[0]
    return top if top in DOMAINS else None


def _violations() -> list[str]:
    out: list[str] = []
    for py in ROOT.rglob("*.py"):
        if py.name.endswith("_test.py") or "/tests/" in str(py):
            continue
        owner = _domain_of(py)
        if owner is None:
            continue
        tree = ast.parse(py.read_text(), filename=str(py))
        for node in ast.walk(tree):
            mod = None
            if isinstance(node, ast.ImportFrom) and node.module:
                mod = node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name
            if not mod:
                continue
            target = mod.split(".")[0]
            if target in DOMAINS and target != owner:
                if mod == f"{target}.api" or mod.startswith(f"{target}.api."):
                    continue
                if (owner, mod) in ALLOW:
                    continue
                out.append(f"{py.relative_to(ROOT)} imports {mod} (use {target}.api)")
    return out


def test_no_cross_domain_internal_imports():
    violations = _violations()
    assert not violations, "Cross-domain boundary violations:\n" + "\n".join(violations)
```

**Step 2: Intended CI outcome:** PASS, because Tasks 1.1-1.4 removed every cross-domain internal import. If CI shows failures, the message lists each offending file -> fix the import to use `<domain>.api`, do not add to `ALLOW` unless there is a documented reason.

**Step 3:** Register `import_boundaries_test.py` in `projects/monolith/BUILD` as a `py_test`.

**Step 4: Commit**

```bash
git add -A
git commit -m "test(monolith): enforce cross-domain imports go through <domain>/api.py"
```

---

### Task 1.6: Phase 1 verification (CI)

**Step 1:** Push the branch and open the PR.

```bash
git push -u origin feat/monolith-modularity
gh pr create --fill
```

**Step 2:** Watch CI: `gh pr checks <n> --watch`. Expected green. The format-bot may auto-commit BUILD/gazelle fixes; pull before further work.

**Step 3:** Diagnose any failure by quoting the CI log (`mcp__buildbuddy__get_invocation` with `commitSha` -> `get_target` -> `get_log`) before hypothesizing. Likely failure classes: a missed `shared.scheduler` import site (the guard or an ImportError will name it), or an unregistered BUILD target for a moved/new file.

**Step 4:** One comprehensive code review of the full Phase 1 diff (per repo cadence), then `gh pr merge --rebase`.

---

# PHASE 2: Roles + public views (item 3)

**Outcome:** A `public_reader` Postgres role that can read only the public surface, defined in an Atlas migration on the primary so it replicates to the future standby. No app wiring yet (the private app keeps using `app`).

**Reference:** ADR 004 "Confidentiality boundary = read-only role plus public views" and the public-endpoint inventory (hikes, ships, stars, public-knowledge, home/observability).

**Key tasks (expand to bite-sized at PR start):**

1. New migration `chart/migrations/<ts>_public_reader_role.sql`: `CREATE ROLE public_reader NOLOGIN;` then `GRANT USAGE ON SCHEMA hikes, ships, stars TO public_reader;` + `GRANT SELECT ON ALL TABLES IN SCHEMA ...` + `ALTER DEFAULT PRIVILEGES ... GRANT SELECT`. Explicitly do NOT grant `knowledge`, `chat`, `home`, `scheduler`, `claude_agent`, `trips`.
2. `knowledge_public` view (in a neutral schema, e.g. `public` or a new `public_api` schema) selecting only `visibility='public'` columns the public graph/notes endpoints need; `GRANT SELECT` on the view to `public_reader`. Define it alongside the gardener's visibility semantics.
3. A login role for the public service to assume (`public_reader` is NOLOGIN group + a `public_svc LOGIN IN ROLE public_reader`, or make `public_reader` LOGIN with a 1Password-managed password). Decide and document; wire the secret via `OnePasswordItem`.
4. Test (SQLite can't model GRANTs; this needs a Postgres-backed CI assertion or an Atlas test): assert a `public_reader` connection cannot `SELECT` from `knowledge.notes` and can `SELECT` from `ships.latest_positions` and `knowledge_public`. If no Postgres CI harness exists, add the assertion as a documented manual verification step against the cluster and note the gap.

**Risks:** view drifts from real `visibility` semantics (ADR 004 risk table) -> co-locate with gardener logic + the negative test. Atlas `atlas.sum` must be regenerated; keep the migrations ConfigMap under the 256 KiB annotation cap (role/view DDL is tiny, fine).

---

# PHASE 3: Read replica (item 4)

**Outcome:** CNPG `instances: 2` with a healthy `monolith-pg-ro` service. Private app/scheduler stay on the primary (`-rw`); nothing reads `-ro` yet.

**Reference:** ADR 004 Layer 3 and its risk row on node memory (prior OOMKills at the 256Mi era, now 1Gi limit).

**Key tasks (expand at PR start):**

1. **Pre-flight capacity check (do first, it gates the phase):** the standby duplicates storage and runs a second Postgres. Verify node memory headroom (`kubectl top nodes`, check for the node hosting `monolith-pg`) and that 50Gi x2 storage fits. The footprint is smaller than ADR 004 assumed: temporal/temporal_visibility/lakehouse DBs are gone, so the standby mirrors only `monolith` + `postgres`.
2. `projects/monolith/deploy/values.yaml`: `postgres.instances: 2`.
3. `chart/templates/cnpg-cluster.yaml`: fix the stale comment on lines 25-29 (drop the "five databases" / Temporal text; state it now hosts `monolith` + the default `postgres`). Confirm CNPG auto-creates the `-ro` service (it does for replica clusters).
4. Bump `chart/Chart.yaml` version + `deploy/application.yaml` `targetRevision` together.
5. Verify post-rollout: `kubectl get cluster monolith-pg -n monolith -o yaml` shows 2 ready instances, `-ro` endpoint resolves, replication lag is low.

**Risks:** memory pressure on a tight node (ADR 004) -> the pre-flight check is mandatory, not optional. Do not point any read-your-writes path at `-ro`.

---

# PHASE 4: ClickHouse decouple via rollup

**Outcome:** The public main page's topology/SLO/GPU tiles read precomputed Postgres snapshots instead of querying ClickHouse at request time, removing the `Semaphore(2)` bottleneck and ClickHouse creds from the eventual public artifact.

**Reference:** ADR 004 Layer 4 and "SLO rollup data flow".

**Key tasks (expand at PR start):**

1. Migration creating `observability.node_slo_snapshot`, `observability.edge_linkerd_snapshot`, `observability.gpu_snapshot` (on primary, replicate to standby).
2. A scheduled job (via `scheduler.api.register_job`, ~15min to match the current topology cache TTL) that runs the existing ClickHouse SLO/edge/GPU queries and writes snapshots. Follow the monolith async-handler rule: network I/O with `await`, then `asyncio.to_thread` for the sync session write (see `projects/monolith/CLAUDE.md`).
3. Repoint `home/observability` read path to the snapshot tables.
4. Tests: snapshot writer (sync core with explicit session, SQLite fixture) + reader returns latest snapshot.

**Risks:** rollup lag leaves the page stale -> 15min cadence matches today's tolerance (ADR 004 risk row, Low/Low).

---

# PHASE 5: Public service split (ADR 004 Layer 1)

**Outcome:** A separately-composed, separately-imaged read-only public service that mounts only the public read routers, connects as `public_reader` on `monolith-pg-ro`, and holds no application secrets. Private modules, write paths, and secrets are absent from the artifact.

**Reference:** ADR 004 Layers 1-4 combined, Security and Risks sections.

**Key tasks (expand at PR start):**

1. `app/main_public.py`: a FastAPI entrypoint importing ONLY the public read routers (hikes, ships, stars, public-knowledge, home/observability) via their `<domain>/api.py` register seams. Private routers (knowledge CRUD, tasks, gaps, chat, scheduler, ingest) are not imported.
2. Import-absence test: assert no private domain module is reachable from `main_public` (mirror the Phase 1 guard, asserting the negative). This is the "shared-code refactor accidentally pulls private modules into public artifact" mitigation from ADR 004.
3. Separate public SvelteKit app serving only public routes; separate apko image; separate chart/deploy.
4. Public service `DATABASE_URL` -> `public_reader` creds on the `-ro` service (no other secrets present).
5. NetworkPolicy NOTE: monolith namespace is Linkerd-meshed -> per `feedback_linkerd_networkpolicy`, do NOT use K8s NetworkPolicies in meshed namespaces (port 4143 mismatch). Reconcile ADR 004's "default-deny NetworkPolicy" against this: use Linkerd authorization policy / the mesh's controls instead, or place the public service in a non-meshed namespace. **This contradicts ADR 004 as written and must be resolved in the Phase 5 PR** (likely an ADR 004 amendment).
6. `readOnlyRootFilesystem: true` on the public container with an `emptyDir` `/tmp` (ADR 004 Security).
7. Repoint the public ingress (ADR 002 path tiers) to the new service.
8. Flip ADR 004 status Draft -> Accepted.

**Risks:** see ADR 004 risk table in full. Most important: the replica is NOT a confidentiality boundary; the `public_reader` role + views (Phase 2) are. Never conflate them.

---

## Cross-cutting notes

- **`infra/` naming (deferred, optional):** Phase 1 keeps the `shared/` name for the surviving infra primitives (`embedding`, `forecast_freshness`, `testing`) to minimize import churn. If a clearer name is wanted later, renaming `shared/` -> `infra/` is a mechanical follow-up, not part of this program.
- **`trips` domain** has no router/jobs yet (in progress per the repo). It owns the `trips` schema; the guard already covers it. No Phase 1 action.
- **`todo` schema** is legacy (kept for back-compat). Not granted to `public_reader`.
