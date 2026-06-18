# Repo-Docs Knowledge-Graph Ingest Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Index the homelab repo's own markdown (docs/, ADRs, plans, project READMEs, CLAUDE.md files) into the knowledge graph so the public-chat RAG can ground answers on project and decision context, without those docs ever entering the curated `knowledge.notes` graph.

**Architecture:** Repo markdown is captured at build time into a committed, diff-friendly NDJSON manifest (path + sha256 + title + content), baked into the monolith image. A scheduler job that runs **only in the private monolith binary** reconciles that manifest against two isolated tables (`knowledge.repo_docs` + `knowledge.repo_doc_chunks`) using content-hash change detection: new paths are chunked + embedded, changed paths are re-chunked + re-embedded in place, and vanished paths are deleted. The public-chat retrieval surface is reached by `UNION ALL`-ing the repo-doc chunks into the existing `public_api.knowledge_chunks` view, so `chat_public/retrieval.py` needs zero changes and inherits the exact same `public_reader`/read-replica confinement the private notes already have.

**Tech Stack:** Python 3 / FastAPI / SQLModel + pgvector (Vector(1024)), Postgres (Atlas migrations), Bazel (`py_venv_binary`, `multirun` format aggregate), the in-cluster `inference-embeddings` endpoint via `shared.embedding.EmbeddingClient`.

---

## Design decisions resolved (read before starting)

These were open questions; here are the settled answers so no task re-litigates them:

1. **Image-baking mechanism = committed NDJSON manifest, not a Bazel filegroup data-layer.** A pure-Bazel data layer was rejected: a root-package `glob` cannot descend into sub-packages (every `projects/*` dir has its own `BUILD`), so capturing `projects/**/*.md` would require adding/maintaining a `filegroup` in dozens of packages. Instead a generator script walks the working tree directly (it runs under `BUILD_WORKSPACE_DIRECTORY`, exactly like `bazel/images/generate-home-cluster.sh`) and emits one NDJSON file. The manifest is the "hash that asserts what changed" from the original ask: each line is one file, sorted by path, so a one-doc edit is a one-line diff.

2. **Manifest stays in sync via the existing `format` aggregate.** The generator is added to `multirun(name = "format", commands = [...])` in `bazel/tools/format/BUILD`, alongside `generate-home-cluster` and `generate-routes`. CI runs `bazel run //bazel/tools/format:format` and `ci-format-bot` auto-commits any regenerated manifest, so a stale manifest self-heals on the PR branch. No separate staleness gate.

3. **Reconcile runs private-only.** `app/main.py` (private binary) calls each domain's `on_startup_jobs(session)` then `run_scheduler_loop()`. `app/main_public.py` deliberately omits the scheduler entirely (see its module docstring + `main_public_imports_test.py`). Registering the job in a new `knowledge.on_startup_jobs` and wiring it into `app/main.py` only is therefore inherently private-only; the public binary never runs it and the manifest file is added to the **private** `:main` binary's `data` only.

4. **Title derivation:** first ATX H1 (`^# (.+)$`) in the body; fallback to the repo-relative path. Computed in the generator so the runtime never re-parses.

5. **Chunk, don't whole-file embed.** Reuse `knowledge/chunker.py::chunk_markdown` (heading-aware, code-fence-safe), one embedding per chunk, mirroring `knowledge/indexing.py`.

6. **pgvector type parity:** mirror the existing `knowledge.models.Chunk` exactly, `embedding: list[float] = Field(sa_column=Column(Vector(1024)))` plus the `@field_validator("embedding", mode="before")` JSON-string parser. That pattern already round-trips through the SQLite `create_all` test fixtures, so the new chunk table inherits the same behaviour for free.

7. **`note_id = 'repo:' || path` is safe.** `chat_public/router.py:276` emits a `node_touched` SSE event per retrieved note by id; the graph overlay silently skips ids that don't match a graph node. Grounding uses only `title` + `chunk_text` (`router.py:70`). So synthetic `repo:` ids degrade gracefully.

8. **Reconcile cadence = 6h (`interval_secs = 21600`).** The manifest only changes when a new image deploys, so between deploys every run is a cheap hash-compare no-op (one `SELECT path, content_hash` + set diff, no embedding). 6h bounds post-deploy staleness without churn. `ttl_secs = 1800`.

9. **Async/sync split (mandatory, `projects/monolith/CLAUDE.md`).** The async handler does the embedding network I/O with `await`; **all** Session I/O is delegated to `asyncio.to_thread` helpers that each open their own `Session(get_engine())`. Two pure sync cores take an explicit `session` param so SQLite fixtures drive them directly. Never pass the scheduler's `session` into `to_thread`; never `session.add` in a loop (use `add_all`). Semgrep `no-sync-session-in-async-def` and `no-session-in-to-thread` enforce this.

10. **Scope = public chat only.** Do NOT union into `public_api.knowledge_notes` (browse page) or `search_notes` (private MCP) in this PR.

### Files touched (overview)

- Create: `projects/monolith/knowledge/repo_docs.py` (manifest loader, reconcile cores, async handler)
- Create: `projects/monolith/knowledge/tools/gen_repo_docs_manifest.py` (generator)
- Create: `projects/monolith/knowledge/repo_docs_manifest.ndjson` (generated, committed)
- Create: `projects/monolith/chart/migrations/20260618120000_repo_docs.sql`
- Modify: `projects/monolith/knowledge/models.py` (RepoDoc + RepoDocChunk)
- Modify: `projects/monolith/knowledge/__init__.py` (`on_startup_jobs`)
- Modify: `projects/monolith/knowledge/api.py` (export reconcile entrypoint)
- Modify: `projects/monolith/app/main.py` (call `knowledge.on_startup_jobs`)
- Modify: `bazel/tools/format/BUILD` (add generator to `format` multirun)
- Modify: `projects/monolith/BUILD` (generator target, manifest in `:main` data, hand-registered tests, `# gazelle:exclude knowledge`)
- Modify: `projects/monolith/chart/migrations/atlas.sum`
- Modify: `projects/monolith/public_api_chunks_grants_test.py` (real-pg view-union test)
- Modify: `projects/monolith/chart/Chart.yaml` + `projects/monolith/deploy/application.yaml` (version bump)
- Test (new): `projects/monolith/knowledge/repo_docs_test.py`, `projects/monolith/knowledge/gen_repo_docs_manifest_test.py`

> **CI note:** there is no local test loop. Implement all tasks, commit per task, push the branch, and watch CI via `gh pr checks <n> --watch`. The SQLite-backed unit tests are the correctness signal for everything except the Postgres-only view union, which `public_api_chunks_grants_test.py` covers against the real-pg harness in CI.

---

## Task 1: Isolated tables, `RepoDoc` + `RepoDocChunk` models

**Files:**

- Modify: `projects/monolith/knowledge/models.py` (add two models near `Chunk`)
- Test: `projects/monolith/knowledge/models_test.py`

**Step 1: Write the failing test**

Add to `models_test.py`:

```python
def test_repo_doc_and_chunk_roundtrip(real_session):
    from knowledge.models import RepoDoc, RepoDocChunk

    doc = RepoDoc(path="docs/security.md", content_hash="abc123", title="Security")
    real_session.add(doc)
    real_session.commit()
    real_session.refresh(doc)

    chunk = RepoDocChunk(
        repo_doc_fk=doc.id,
        chunk_index=0,
        section_header="# Security",
        chunk_text="never hardcode secrets",
        embedding=[0.1] * 1024,
    )
    real_session.add(chunk)
    real_session.commit()
    real_session.refresh(chunk)

    assert chunk.repo_doc_fk == doc.id
    assert len(chunk.embedding) == 1024
    assert doc.path == "docs/security.md"
```

(Use whatever the in-repo SQLite session fixture is named, match the other tests in `models_test.py`, e.g. `real_session` / `session`.)

**Step 2: Run to verify it fails**

CI (push), expected: `ImportError: cannot import name 'RepoDoc'`.

**Step 3: Implement the models**

In `knowledge/models.py`, after the `Chunk` class, add:

```python
class RepoDoc(SQLModel, table=True):
    """A repo markdown file indexed for public-chat grounding.

    Isolated from ``Note`` on purpose: the gardener and gap loop operate over
    ``knowledge.notes`` and must never touch these machine-synced, fully
    reconstructable rows. Identified by repo-relative ``path``; ``content_hash``
    is the change-detection key driving the reconcile job.

    Mirrors chart/migrations/20260618120000_repo_docs.sql, keep in sync.
    """

    __tablename__ = "repo_docs"
    __table_args__ = {"schema": "knowledge", "extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    path: str = Field(sa_column=Column(String, nullable=False, unique=True))
    content_hash: str
    title: str
    indexed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class RepoDocChunk(SQLModel, table=True):
    """One embedded chunk of a RepoDoc. Mirrors knowledge.Chunk's embedding
    column exactly so it round-trips through the SQLite create_all fixtures.

    Mirrors chart/migrations/20260618120000_repo_docs.sql, keep in sync.
    """

    __tablename__ = "repo_doc_chunks"
    __table_args__ = {"schema": "knowledge", "extend_existing": True}

    id: int | None = Field(default=None, primary_key=True)
    repo_doc_fk: int = Field(foreign_key="knowledge.repo_docs.id")
    chunk_index: int
    section_header: str = ""
    chunk_text: str
    embedding: list[float] = Field(sa_column=Column(Vector(1024)))

    @field_validator("embedding", mode="before")
    @classmethod
    def _parse_embedding(cls, v: object) -> object:
        if isinstance(v, str):
            return json.loads(v)
        return v
```

(`Vector`, `Column`, `String`, `Field`, `SQLModel`, `field_validator`, `json`, `datetime`, `timezone` are all already imported at the top of `models.py`.)

**Step 4: Run to verify it passes** (push / CI). Expected: PASS.

**Step 5: Commit**

```bash
git add projects/monolith/knowledge/models.py projects/monolith/knowledge/models_test.py
git commit -m "feat(knowledge): add isolated repo_docs + repo_doc_chunks models"
```

---

## Task 2: Manifest generator

A standalone script that walks the working tree and writes the NDJSON manifest. Pure stdlib so it can run as a thin `py_venv_binary` and be unit-tested against a temp tree.

**Files:**

- Create: `projects/monolith/knowledge/tools/gen_repo_docs_manifest.py`
- Test: `projects/monolith/knowledge/gen_repo_docs_manifest_test.py`

**Step 1: Write the failing test**

```python
import json
from pathlib import Path

from knowledge.tools.gen_repo_docs_manifest import (
    derive_title,
    iter_doc_paths,
    build_manifest_lines,
)


def test_derive_title_prefers_h1():
    assert derive_title("# Hello\n\nbody", "docs/x.md") == "Hello"


def test_derive_title_falls_back_to_path():
    assert derive_title("no heading here", "docs/x.md") == "docs/x.md"


def test_iter_doc_paths_includes_and_excludes(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("# A")
    (tmp_path / "projects" / "svc").mkdir(parents=True)
    (tmp_path / "projects" / "svc" / "README.md").write_text("# R")
    (tmp_path / "CLAUDE.md").write_text("# Root")
    # excluded noise
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "z.md").write_text("# Z")
    (tmp_path / "projects" / "svc" / "frontend").mkdir()
    (tmp_path / "projects" / "svc" / "frontend" / "build").mkdir()
    (tmp_path / "projects" / "svc" / "frontend" / "build" / "g.md").write_text("# G")

    paths = set(iter_doc_paths(tmp_path))
    assert "docs/a.md" in paths
    assert "projects/svc/README.md" in paths
    assert "CLAUDE.md" in paths
    assert "node_modules/z.md" not in paths
    assert "projects/svc/frontend/build/g.md" not in paths


def test_build_manifest_lines_sorted_ndjson(tmp_path: Path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "b.md").write_text("# B\n\nbeta")
    (tmp_path / "docs" / "a.md").write_text("# A\n\nalpha")

    lines = build_manifest_lines(tmp_path)
    objs = [json.loads(line) for line in lines]
    assert [o["path"] for o in objs] == ["docs/a.md", "docs/b.md"]  # sorted
    assert objs[0]["title"] == "A"
    assert objs[0]["content"] == "# A\n\nalpha"
    assert len(objs[0]["sha256"]) == 64
```

**Step 2: Run to verify it fails** (push), `ModuleNotFoundError`.

**Step 3: Implement the generator**

`knowledge/tools/gen_repo_docs_manifest.py`:

```python
"""Generate the repo-docs manifest baked into the monolith image.

Walks the working tree (under BUILD_WORKSPACE_DIRECTORY when run via `bazel run`,
mirroring bazel/images/generate-home-cluster.sh) and writes one NDJSON line per
indexed markdown file, sorted by repo-relative path, to
projects/monolith/knowledge/repo_docs_manifest.ndjson.

This is run as part of the `//bazel/tools/format:format` multirun, so CI's format
check regenerates and ci-format-bot auto-commits it whenever a doc changes. The
private monolith's reconcile job reads the committed manifest from the image.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Iterator

MANIFEST_REL = "projects/monolith/knowledge/repo_docs_manifest.ndjson"

# Top-level dir prefixes / filenames we index.
_INCLUDE_GLOBS = ("docs/**/*.md", "projects/**/*.md")
_INCLUDE_NAMES = ("CLAUDE.md",)  # plus any nested CLAUDE.md (matched by name)

# Path segments that mark generated / vendored / irrelevant trees.
_EXCLUDE_SEGMENTS = (
    "node_modules",
    ".git",
    "_trash",
    "/build/",
    "/dist/",
    "/.svelte-kit/",
    "vendor",
)


_H1 = re.compile(r"^#\s+(.+?)\s*$", re.MULTILINE)


def derive_title(content: str, rel_path: str) -> str:
    m = _H1.search(content)
    return m.group(1).strip() if m else rel_path


def _excluded(rel_path: str) -> bool:
    p = f"/{rel_path}/"
    return any(seg in p for seg in _EXCLUDE_SEGMENTS) or rel_path == MANIFEST_REL


def iter_doc_paths(root: Path) -> Iterator[str]:
    seen: set[str] = set()
    for pattern in _INCLUDE_GLOBS:
        for fp in root.glob(pattern):
            if not fp.is_file():
                continue
            rel = fp.relative_to(root).as_posix()
            if not _excluded(rel):
                seen.add(rel)
    # CLAUDE.md anywhere (root + nested), which the project-glob may miss at root.
    for fp in root.rglob("CLAUDE.md"):
        if not fp.is_file():
            continue
        rel = fp.relative_to(root).as_posix()
        if not _excluded(rel):
            seen.add(rel)
    yield from sorted(seen)


def build_manifest_lines(root: Path) -> list[str]:
    lines: list[str] = []
    for rel in iter_doc_paths(root):
        content = (root / rel).read_text(encoding="utf-8")
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        obj = {
            "path": rel,
            "sha256": sha,
            "title": derive_title(content, rel),
            "content": content,
        }
        # sort_keys for a stable, diff-friendly serialization.
        lines.append(json.dumps(obj, ensure_ascii=False, sort_keys=True))
    return lines


def main() -> int:
    root = Path(os.environ.get("BUILD_WORKSPACE_DIRECTORY") or os.getcwd())
    out = root / MANIFEST_REL
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = build_manifest_lines(root)
    out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    print(f"wrote {len(lines)} docs to {MANIFEST_REL}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

> Decision: `_INCLUDE_GLOBS` includes `docs/**/*.md` (covers `docs/decisions/` ADRs and `docs/plans/`). If you later find plan/scratch docs add noise, narrow it, but include them for now; they are useful grounding.

**Step 4: Run to verify it passes** (push), PASS.

**Step 5: Commit**

```bash
git add projects/monolith/knowledge/tools/gen_repo_docs_manifest.py \
        projects/monolith/knowledge/gen_repo_docs_manifest_test.py
git commit -m "feat(knowledge): add repo-docs manifest generator"
```

---

## Task 3: Wire the generator into Bazel + format, generate the manifest

**Files:**

- Modify: `bazel/tools/format/BUILD` (add target to the `format` multirun)
- Modify: `projects/monolith/BUILD` (define `gen_repo_docs_manifest` binary; hand-register the two new tests, recall `# gazelle:exclude knowledge`)
- Create (generated): `projects/monolith/knowledge/repo_docs_manifest.ndjson`

**Step 1: Define the generator binary** in `projects/monolith/BUILD` (near the other `py_venv_binary` tool targets like `add_visibility_field`):

```python
py_venv_binary(
    name = "gen_repo_docs_manifest",
    main = "knowledge/tools/gen_repo_docs_manifest.py",
    imports = ["."],  # keep
    visibility = ["//:__subpackages__"],
)
```

**Step 2: Hand-register the new unit tests** in `projects/monolith/BUILD` (mirror an existing knowledge `py_test`/`py_pytest_test` entry, copy the exact macro + deps another knowledge test uses, e.g. the one for `models_test`). Add entries for `repo_docs_test` (Task 5) and `gen_repo_docs_manifest_test` (Task 2). Because of `# gazelle:exclude knowledge`, gazelle will NOT generate these, they must be written by hand, matching a sibling test's structure exactly.

**Step 3: Add the generator to the `format` multirun** in `bazel/tools/format/BUILD`:

```python
multirun(
    name = "format",
    commands = [
        "//bazel/images:generate-home-cluster",
        "//bazel/images:generate-push-all",
        "//bazel/images:generate-push-all-pages",
        "//projects/monolith:generate-routes",
        "//projects/monolith:gen_repo_docs_manifest",  # add this line
        ":format_code",
        "//:gazelle",
    ],
    jobs = 10,
)
```

**Step 4: Generate the manifest locally and commit it**

```bash
cd /tmp/claude-worktrees/repo-docs-kg-ingest
BUILD_WORKSPACE_DIRECTORY="$PWD" python projects/monolith/knowledge/tools/gen_repo_docs_manifest.py
wc -l projects/monolith/knowledge/repo_docs_manifest.ndjson   # sanity: one line per doc
```

(Running the script directly with the env var set produces the identical output the Bazel target will; you do not need Bazel locally.)

**Step 5: Bake the manifest into the PRIVATE image only.** In `projects/monolith/BUILD`, add the manifest to the `:main` `py_venv_binary`'s `data` (NOT `monolith_backend`, to keep the public image lean):

```python
    data = [
        ":frontend_dist",
        "knowledge/repo_docs_manifest.ndjson",
    ],
```

**Step 6: Commit**

```bash
git add bazel/tools/format/BUILD projects/monolith/BUILD \
        projects/monolith/knowledge/repo_docs_manifest.ndjson
git commit -m "build(knowledge): generate repo-docs manifest in format, bake into private image"
```

---

## Task 4: Manifest loader (runtime)

A small runtime module function that reads the baked manifest from the image. Put it in the module created next task; this task is the loader + its test, written first.

**Files:**

- Create: `projects/monolith/knowledge/repo_docs.py` (loader portion)
- Test: `projects/monolith/knowledge/repo_docs_test.py`

**Step 1: Write the failing test**

```python
import json
from pathlib import Path

from knowledge.repo_docs import ManifestEntry, load_manifest


def test_load_manifest_parses_ndjson(tmp_path: Path):
    p = tmp_path / "m.ndjson"
    p.write_text(
        "\n".join(
            json.dumps(o, sort_keys=True)
            for o in [
                {"path": "docs/a.md", "sha256": "h1", "title": "A", "content": "# A"},
                {"path": "CLAUDE.md", "sha256": "h2", "title": "Root", "content": "# Root"},
            ]
        )
        + "\n"
    )
    entries = load_manifest(p)
    assert [e.path for e in entries] == ["docs/a.md", "CLAUDE.md"]
    assert entries[0] == ManifestEntry(path="docs/a.md", sha256="h1", title="A", content="# A")


def test_load_manifest_missing_file_returns_empty(tmp_path: Path):
    assert load_manifest(tmp_path / "nope.ndjson") == []
```

**Step 2: Run to verify it fails** (push).

**Step 3: Implement the loader** (start `knowledge/repo_docs.py`):

```python
"""Repo-docs ingest: reconcile the baked markdown manifest into isolated
knowledge.repo_docs / repo_doc_chunks tables for public-chat grounding.

Confinement, isolation, and the async/sync split are documented inline at the
relevant functions. This module is imported only by the private binary's
scheduler wiring; the public binary never runs the reconcile.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# The manifest sits beside this module in the image runfiles (Task 3 adds it to
# the :main binary's data). An env override exists purely for tests / ops.
_MANIFEST_NAME = "repo_docs_manifest.ndjson"


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    sha256: str
    title: str
    content: str


def manifest_path() -> Path:
    import os

    override = os.environ.get("REPO_DOCS_MANIFEST_PATH")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / _MANIFEST_NAME


def load_manifest(path: Path | None = None) -> list[ManifestEntry]:
    p = path or manifest_path()
    if not p.exists():
        logger.warning("repo_docs: manifest not found at %s; nothing to index", p)
        return []
    entries: list[ManifestEntry] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        o = json.loads(line)
        entries.append(
            ManifestEntry(
                path=o["path"], sha256=o["sha256"], title=o["title"], content=o["content"]
            )
        )
    return entries
```

**Step 4: Run to verify it passes** (push).

**Step 5: Commit**

```bash
git add projects/monolith/knowledge/repo_docs.py projects/monolith/knowledge/repo_docs_test.py
git commit -m "feat(knowledge): repo-docs manifest loader"
```

---

## Task 5: Reconcile cores (pure, SQLite-testable)

Two sync cores: `plan_reconcile` (read current hashes, diff, chunk new/changed docs) and `apply_reconcile` (upsert docs + chunks, delete vanished). Both take an explicit `session` so SQLite fixtures drive them. Embedding happens between them, in the async handler (Task 6).

**Files:**

- Modify: `projects/monolith/knowledge/repo_docs.py`
- Modify: `projects/monolith/knowledge/repo_docs_test.py`

**Step 1: Write failing tests** (append to `repo_docs_test.py`). Use the same SQLite session fixture the other knowledge tests use.

```python
from knowledge.models import RepoDoc, RepoDocChunk
from knowledge.repo_docs import ManifestEntry, ReconcilePlan, apply_reconcile, plan_reconcile


def _entry(path, content, sha):
    return ManifestEntry(path=path, sha256=sha, title=path, content=content)


def test_plan_reconcile_classifies_new_changed_deleted(real_session):
    # Seed an existing doc that will be (a) unchanged and (b) one that vanishes.
    real_session.add(RepoDoc(path="keep.md", content_hash="same", title="keep"))
    real_session.add(RepoDoc(path="gone.md", content_hash="x", title="gone"))
    real_session.commit()

    entries = [
        _entry("keep.md", "# keep", "same"),       # unchanged -> skip
        _entry("new.md", "# new\n\nbody", "n1"),   # new -> index
    ]
    plan = plan_reconcile(real_session, entries)

    assert {e.path for e, _ in plan.to_upsert} == {"new.md"}
    assert plan.to_delete == ["gone.md"]
    # chunks were produced for the new doc
    _, chunks = plan.to_upsert[0]
    assert chunks and chunks[0]["text"]


def test_plan_reconcile_detects_changed_hash(real_session):
    real_session.add(RepoDoc(path="c.md", content_hash="old", title="c"))
    real_session.commit()
    plan = plan_reconcile(real_session, [_entry("c.md", "# c\n\nnew text", "new")])
    assert {e.path for e, _ in plan.to_upsert} == {"c.md"}


def test_apply_reconcile_inserts_and_embeds(real_session):
    entry = _entry("docs/x.md", "# X\n\nalpha", "h")
    chunks = [{"index": 0, "section_header": "# X", "text": "alpha"}]
    plan = ReconcilePlan(to_upsert=[(entry, chunks)], to_delete=[])
    vectors_by_path = {"docs/x.md": [[0.2] * 1024]}

    apply_reconcile(real_session, plan, vectors_by_path)

    doc = real_session.query(RepoDoc).filter_by(path="docs/x.md").one()
    assert doc.content_hash == "h" and doc.title == "X" or doc.title == "docs/x.md"
    rows = real_session.query(RepoDocChunk).filter_by(repo_doc_fk=doc.id).all()
    assert len(rows) == 1 and len(rows[0].embedding) == 1024


def test_apply_reconcile_replaces_chunks_on_change(real_session):
    # first index
    e1 = _entry("y.md", "# Y\n\nv1", "h1")
    apply_reconcile(
        real_session,
        ReconcilePlan(to_upsert=[(e1, [{"index": 0, "section_header": "", "text": "v1"}])], to_delete=[]),
        {"y.md": [[0.1] * 1024]},
    )
    # re-index with changed content -> old chunks gone, new chunk present, hash updated
    e2 = _entry("y.md", "# Y\n\nv2 longer", "h2")
    apply_reconcile(
        real_session,
        ReconcilePlan(to_upsert=[(e2, [{"index": 0, "section_header": "", "text": "v2 longer"}])], to_delete=[]),
        {"y.md": [[0.9] * 1024]},
    )
    doc = real_session.query(RepoDoc).filter_by(path="y.md").one()
    rows = real_session.query(RepoDocChunk).filter_by(repo_doc_fk=doc.id).all()
    assert doc.content_hash == "h2"
    assert len(rows) == 1 and rows[0].chunk_text == "v2 longer"


def test_apply_reconcile_deletes_vanished_doc_and_chunks(real_session):
    e = _entry("z.md", "# Z\n\nzz", "h")
    apply_reconcile(
        real_session,
        ReconcilePlan(to_upsert=[(e, [{"index": 0, "section_header": "", "text": "zz"}])], to_delete=[]),
        {"z.md": [[0.3] * 1024]},
    )
    doc_id = real_session.query(RepoDoc).filter_by(path="z.md").one().id
    apply_reconcile(real_session, ReconcilePlan(to_upsert=[], to_delete=["z.md"]), {})
    assert real_session.query(RepoDoc).filter_by(path="z.md").first() is None
    assert real_session.query(RepoDocChunk).filter_by(repo_doc_fk=doc_id).count() == 0
```

**Step 2: Run to verify they fail** (push).

**Step 3: Implement the cores** (append to `repo_docs.py`):

```python
from knowledge.chunker import Chunk, chunk_markdown


@dataclass
class ReconcilePlan:
    to_upsert: list[tuple[ManifestEntry, list[Chunk]]]
    to_delete: list[str]


@dataclass
class ReconcileStats:
    upserted: int
    deleted: int
    unchanged: int


def _title_for(entry: ManifestEntry) -> str:
    # The generator already derived the title; trust it (fallback already applied).
    return entry.title


def plan_reconcile(session, entries: list[ManifestEntry]) -> ReconcilePlan:
    """Pure diff: compare manifest hashes to the stored hashes and chunk the docs
    that need (re)indexing. No embedding (network) and no writes happen here.
    """
    from knowledge.models import RepoDoc

    existing: dict[str, str] = {
        path: h for path, h in session.query(RepoDoc.path, RepoDoc.content_hash).all()
    }
    manifest_paths = {e.path for e in entries}

    to_upsert: list[tuple[ManifestEntry, list[Chunk]]] = []
    for e in entries:
        if existing.get(e.path) == e.sha256:
            continue  # unchanged
        chunks = chunk_markdown(e.content)
        if not chunks:
            chunks = [{"index": 0, "section_header": "", "text": e.content or e.title}]
        to_upsert.append((e, chunks))

    to_delete = sorted(p for p in existing if p not in manifest_paths)
    return ReconcilePlan(to_upsert=to_upsert, to_delete=to_delete)


def apply_reconcile(
    session, plan: ReconcilePlan, vectors_by_path: dict[str, list[list[float]]]
) -> ReconcileStats:
    """Apply the plan in one transaction: delete vanished docs (+ their chunks),
    and upsert changed/new docs replacing their chunk set. ``vectors_by_path``
    holds one embedding per chunk, in chunk order, keyed by doc path.
    """
    from knowledge.models import RepoDoc, RepoDocChunk

    deleted = 0
    for path in plan.to_delete:
        doc = session.query(RepoDoc).filter_by(path=path).first()
        if doc is None:
            continue
        session.query(RepoDocChunk).filter_by(repo_doc_fk=doc.id).delete()
        session.delete(doc)
        deleted += 1

    upserted = 0
    for entry, chunks in plan.to_upsert:
        vectors = vectors_by_path.get(entry.path) or []
        doc = session.query(RepoDoc).filter_by(path=entry.path).first()
        if doc is None:
            doc = RepoDoc(path=entry.path, content_hash=entry.sha256, title=_title_for(entry))
            session.add(doc)
            session.flush()  # assign doc.id
        else:
            doc.content_hash = entry.sha256
            doc.title = _title_for(entry)
            session.query(RepoDocChunk).filter_by(repo_doc_fk=doc.id).delete()
            session.flush()
        rows = [
            RepoDocChunk(
                repo_doc_fk=doc.id,
                chunk_index=c["index"],
                section_header=c["section_header"],
                chunk_text=c["text"],
                embedding=vectors[i] if i < len(vectors) else [0.0] * 1024,
            )
            for i, c in enumerate(chunks)
        ]
        session.add_all(rows)  # never session.add in a loop (semgrep session-add-in-loop)
        upserted += 1

    session.commit()
    return ReconcileStats(upserted=upserted, deleted=deleted, unchanged=0)
```

> Note: `apply_reconcile` is given pre-computed embeddings; it does no network I/O, so it is safe to run inside `to_thread`. The `[0.0] * 1024` fallback only triggers if the embedder returned fewer vectors than chunks (defensive; logged upstream).

**Step 4: Run to verify they pass** (push).

**Step 5: Commit**

```bash
git add projects/monolith/knowledge/repo_docs.py projects/monolith/knowledge/repo_docs_test.py
git commit -m "feat(knowledge): repo-docs reconcile plan/apply cores"
```

---

## Task 6: Async handler + scheduler wiring (private-only)

**Files:**

- Modify: `projects/monolith/knowledge/repo_docs.py` (async handler)
- Modify: `projects/monolith/knowledge/__init__.py` (`on_startup_jobs`)
- Modify: `projects/monolith/knowledge/api.py` (export the handler/entrypoint)
- Modify: `projects/monolith/app/main.py` (call `knowledge.on_startup_jobs(session)`)

**Step 1: Implement the async handler** (append to `repo_docs.py`). It obeys `projects/monolith/CLAUDE.md`: network embedding with `await`, all Session I/O in `to_thread` helpers that open their own session.

```python
import asyncio
from datetime import datetime, timezone


def _plan_in_thread(entries: list[ManifestEntry]) -> ReconcilePlan:
    from sqlmodel import Session

    from app.db import get_engine

    with Session(get_engine()) as session:
        return plan_reconcile(session, entries)


def _apply_in_thread(plan: ReconcilePlan, vectors_by_path) -> ReconcileStats:
    from sqlmodel import Session

    from app.db import get_engine

    with Session(get_engine()) as session:
        return apply_reconcile(session, plan, vectors_by_path)


async def repo_docs_reconcile_handler(session) -> datetime | None:
    """Scheduler handler (private binary only). Diff the baked manifest against the
    DB, embed the changed docs' chunks, and apply. The ``session`` arg is the
    scheduler's loop session and is intentionally NOT used for I/O here (semgrep
    no-session-in-to-thread): every DB touch happens in its own threaded session.
    """
    from shared.embedding import EmbeddingClient

    entries = load_manifest()
    if not entries:
        return None

    plan = await asyncio.to_thread(_plan_in_thread, entries)
    if not plan.to_upsert and not plan.to_delete:
        logger.info("repo_docs: nothing to reconcile (manifest unchanged)")
        return None

    client = EmbeddingClient()
    vectors_by_path: dict[str, list[list[float]]] = {}
    for entry, chunks in plan.to_upsert:
        texts = [c["text"] for c in chunks]
        try:
            vectors_by_path[entry.path] = await client.embed_batch(texts)
        except Exception:  # noqa: BLE001 - skip this doc; next run retries it
            logger.exception("repo_docs: embedding failed for %s; skipping", entry.path)

    # Drop upserts whose embedding failed so we never persist zero-vectors; their
    # hash stays unchanged in the DB so the next run retries them.
    plan.to_upsert = [(e, c) for (e, c) in plan.to_upsert if e.path in vectors_by_path]

    stats = await asyncio.to_thread(_apply_in_thread, plan, vectors_by_path)
    logger.info(
        "repo_docs: reconciled upserted=%d deleted=%d", stats.upserted, stats.deleted
    )
    return None
```

> The handler is thin (network + `to_thread`) and is not unit-tested directly, per the CLAUDE.md pattern; the sync cores carry the coverage. Confirm `EmbeddingClient.embed_batch` is the correct method name (see `knowledge/indexing.py::_Embedder`).

**Step 2: Add `on_startup_jobs`** to `knowledge/__init__.py`:

```python
def on_startup_jobs(session) -> None:
    """Register knowledge scheduled jobs (private binary only).

    The public binary never calls this (app/main_public.py runs no scheduler),
    so the repo-docs reconcile, which writes the knowledge schema, only ever runs
    where there is write access.
    """
    from knowledge.repo_docs import repo_docs_reconcile_handler
    from scheduler.api import register_job

    register_job(
        session,
        name="knowledge.repo_docs_reconcile",
        interval_secs=21600,  # 6h; no-op hash compare between deploys
        handler=repo_docs_reconcile_handler,
        ttl_secs=1800,
    )
```

Add `"on_startup_jobs"` to `knowledge/__init__.py`'s `__all__`.

**Step 3: Wire into the private binary.** In `projects/monolith/app/main.py`, in the block that calls `*.on_startup_jobs(session)` (currently home/ships/hikes/stars/dr_jobs, around line 69-73), add:

```python
        knowledge.on_startup_jobs(session)
```

(Confirm `knowledge` is imported in `main.py`, it is, given `knowledge.register(app)` at line 206.)

**Step 4: Export via `knowledge.api`** if the import-boundary test requires cross-domain visibility. The handler is only referenced inside the `knowledge` package + `app/main.py` (which may import internals freely, verify against `import_boundaries_test`). If `app/main.py` is treated as a separate domain by that test, re-export `on_startup_jobs` is already on the package; nothing else is needed. Do NOT add `repo_docs` internals to `api.py` unless the boundary test fails on push, keep the surface minimal.

**Step 5: Verify on push.** Expected CI signal: `main_public_imports_test` still passes (no scheduler in public), `import_boundaries_test` passes, semgrep `no-sync-session-in-async-def` / `no-session-in-to-thread` pass (the handler does no sync session work and passes only plain data into `to_thread`).

**Step 6: Commit**

```bash
git add projects/monolith/knowledge/repo_docs.py projects/monolith/knowledge/__init__.py \
        projects/monolith/knowledge/api.py projects/monolith/app/main.py
git commit -m "feat(knowledge): private-only repo-docs reconcile scheduler job"
```

---

## Task 7: Migration, tables + view union

**Files:**

- Create: `projects/monolith/chart/migrations/20260618120000_repo_docs.sql`
- Modify: `projects/monolith/chart/migrations/atlas.sum`

**Step 1: Write the migration**

```sql
-- Repo-docs ingest (public-chat grounding). Two ISOLATED tables, deliberately
-- outside the curated knowledge.notes graph so the gardener and gap loop never
-- touch these machine-synced, fully reconstructable rows. The private monolith's
-- knowledge.repo_docs_reconcile job upserts/deletes them from the image-baked
-- manifest by content hash.

CREATE TABLE knowledge.repo_docs (
    id            SERIAL PRIMARY KEY,
    path          TEXT NOT NULL UNIQUE,
    content_hash  TEXT NOT NULL,
    title         TEXT NOT NULL,
    indexed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE knowledge.repo_doc_chunks (
    id              SERIAL PRIMARY KEY,
    repo_doc_fk     INTEGER NOT NULL REFERENCES knowledge.repo_docs(id) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,
    section_header  TEXT NOT NULL DEFAULT '',
    chunk_text      TEXT NOT NULL,
    embedding       vector(1024) NOT NULL
);

CREATE INDEX repo_doc_chunks_doc_idx ON knowledge.repo_doc_chunks (repo_doc_fk);

-- Surface repo docs to the public-chat retrieval path by UNION-ing them into the
-- existing public chunk view. Same columns/types/order as the original (Phase 4a,
-- 20260617040000) so CREATE OR REPLACE is valid and the public_reader GRANT is
-- preserved. Synthetic note_id 'repo:'||path never collides with a real note_id;
-- the retrieval grounding uses title + chunk_text, and the chat graph overlay
-- silently ignores ids with no matching graph node.
CREATE OR REPLACE VIEW public_api.knowledge_chunks AS
    SELECT
        n.note_id        AS note_id,
        n.title          AS title,
        c.chunk_index    AS chunk_index,
        c.section_header AS section_header,
        c.chunk_text     AS chunk_text,
        c.embedding      AS embedding
    FROM knowledge.chunks c
    JOIN knowledge.notes n ON c.note_fk = n.id
    WHERE n.visibility = 'public'
      AND n.deleted_at IS NULL
    UNION ALL
    SELECT
        'repo:' || d.path AS note_id,
        d.title           AS title,
        rc.chunk_index    AS chunk_index,
        rc.section_header AS section_header,
        rc.chunk_text     AS chunk_text,
        rc.embedding      AS embedding
    FROM knowledge.repo_doc_chunks rc
    JOIN knowledge.repo_docs d ON rc.repo_doc_fk = d.id;

GRANT SELECT ON public_api.knowledge_chunks TO public_reader;
```

> The repo-doc base tables need no direct `public_reader` GRANT: the view is not `security_invoker`, so it reads `knowledge.*` with the owner's privileges (same pattern as the existing public_api views). `public_reader` only ever selects the view.

**Step 2: Update `atlas.sum`.** Regenerate the migration directory hash the same way the prior migrations did (the file ends in an `atlas.sum` with a hash line per migration). Use the repo's Atlas tooling, check how `20260618000000_dr_jobs_public_reader_grant.sql` was hashed (likely `atlas migrate hash --dir file://projects/monolith/chart/migrations`). If Atlas is not vendored locally, push and let CI surface the expected sum, then paste it in; the Atlas operator validates `atlas.sum` against the file set at apply time, and CI's migration lint will fail loudly with the expected hash if it is wrong. Do not hand-edit hash bytes blindly.

**Step 3: Commit**

```bash
git add projects/monolith/chart/migrations/20260618120000_repo_docs.sql \
        projects/monolith/chart/migrations/atlas.sum
git commit -m "feat(knowledge): repo_docs tables + union into public chunk view"
```

---

## Task 8: Real-Postgres view-union test

**Files:**

- Modify: `projects/monolith/public_api_chunks_grants_test.py`

**Step 1: Add a test** mirroring the existing public-chunk grant test: seed a `knowledge.repo_docs` + `knowledge.repo_doc_chunks` row, then SELECT `public_api.knowledge_chunks` as `public_reader` and assert the `repo:`-prefixed row is returned with its embedding, and that a private note's chunk still is not. Match the harness/fixtures already used in that file (it is the real-pg test added in `20260617040000`). Keep the assertions parallel to the existing ones.

**Step 2: Verify on push** (this test only runs against the real-pg CI harness, not SQLite).

**Step 3: Commit**

```bash
git add projects/monolith/public_api_chunks_grants_test.py
git commit -m "test(knowledge): public chunk view returns repo-doc chunks for public_reader"
```

---

## Task 9: Chart version bump

**Files:**

- Modify: `projects/monolith/chart/Chart.yaml` (bump `version`)
- Modify: `projects/monolith/deploy/application.yaml` (bump `targetRevision` to match)

**Step 1:** Bump the chart `version` (next patch/minor from the current value) and set `deploy/application.yaml`'s `targetRevision` to the identical string. They MUST match or ArgoCD keeps deploying the old chart (and never the new migration/job). The `chart-version-bot` normally syncs these, but bump both by hand here so the PR is self-consistent.

**Step 2: Commit**

```bash
git add projects/monolith/chart/Chart.yaml projects/monolith/deploy/application.yaml
git commit -m "chore(monolith): bump chart version for repo-docs ingest"
```

---

## Task 10: Push, watch CI, verify live

**Step 1: Push + open PR**

```bash
git push -u origin feat/repo-docs-kg-ingest
gh pr create --fill
gh pr checks <number> --watch
```

**Step 2: Diagnose any red CI** by quoting the actual failure (`mcp__buildbuddy__get_invocation` with the commit SHA → `get_target` → `get_log`). Likely first-failure suspects, in order:

- Hand-registered BUILD test entries missing/mismatched (gazelle won't help, `# gazelle:exclude knowledge`).
- `atlas.sum` mismatch (paste the expected hash CI prints).
- Semgrep on the async handler (re-check: no sync `session.*` calls in the `async def`; only plain data into `to_thread`).
- `main_public_imports_test` if anything pulled the scheduler/reconcile into the public import graph.

**Step 3: After merge, verify live** (per CLAUDE.md GitOps): once ArgoCD syncs the new chart, the private monolith registers `knowledge.repo_docs_reconcile`. Check it ran and populated rows:

```bash
# scheduler row exists
kubectl exec -n <monolith-ns> <monolith-pod> -- \
  python -c "from app.db import get_engine; from sqlmodel import Session, text; \
import os; s=Session(get_engine()); \
print(s.exec(text('select count(*) from knowledge.repo_docs')).one()); \
print(s.exec(text('select count(*) from knowledge.repo_doc_chunks')).one())"
```

(Or use the `/scheduler` skill / `monolith-monolith-agent-*` MCP tools to trigger `knowledge.repo_docs_reconcile` on the next tick rather than waiting up to 6h.)

**Step 4: Verify the public chat grounds on a repo doc.** Ask the public chat a question whose answer lives only in a repo doc (e.g. "what container build tool does the homelab use?" → should ground on the apko anti-pattern). Confirm a `node_touched` event with a `repo:`-prefixed id appears and the answer reflects the doc.

---

## Out of scope (explicit follow-ons, not this PR)

- Union repo docs into `public_api.knowledge_notes` so they appear as nodes on `/app/notes` (browse + graph).
- Union into `search_notes` so the private MCP `search-knowledge` returns repo docs in agent sessions.
- A `repo:`-id resolver in the public frontend so a grounded repo source is clickable (currently it just doesn't highlight a graph node, which is fine).
- Trimming which `docs/plans/*` get indexed if they prove noisy.
