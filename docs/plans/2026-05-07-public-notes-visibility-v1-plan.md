# Public Notes — Visibility Labels & Public Surface (V1) — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a binary `visibility: public | private` label to every note, expose a strictly filtered public knowledge-graph + note API at `/api/knowledge/public/...`, and ship two vault-mutation scripts (mechanical add + LLM backfill).

**Architecture:** Frontmatter is canonical (the vault); a nullable `visibility` column on `knowledge.notes` mirrors it (null is treated as private). A new `knowledge/visibility.py` module owns the load-bearing artifacts: the criteria prompt block, the coalescing rule, the wikilink sanitizer, and the SQL filter. Two new public endpoints reuse the existing graph/note serializers but route through that single helper. Vault scripts under `knowledge/tools/` migrate disk state in two phases — schema-first (mechanical), values-second (LLM).

**Tech Stack:** Python 3.12, FastAPI, SQLModel + SQLAlchemy, PostgreSQL (Atlas migrations), Bazel + aspect_rules_py, the `claude` CLI subprocess (per `memory/feedback_claude_cli_subprocess_for_tos.md` — never the Anthropic SDK).

**Design doc:** `docs/plans/2026-05-07-public-notes-visibility-design.md` — read it first for full context.

---

## Plan-wide notes

- **No local test loop.** Per `CLAUDE.md`, do not run `bazel test` from the workstation. Each task ends with `git commit`. Verification is the **single PR's CI run** at the end of the plan. The `format` command (vendored, in-direnv) updates BUILD files via gazelle and must be run before every commit that adds a new `.py` file.
- **Single PR.** All 14 tasks land on the `feat/public-notes-visibility` branch (already created), one PR opened in Task 14.
- **Code review cadence:** per repo convention, **one** comprehensive code review at end-of-PR, not per-task. Implementer subagents self-review; no per-task reviewer dispatch.
- **Conventional Commits.** A `commit-msg` hook enforces format. Use `feat`, `fix`, `docs`, `test`, `refactor`, `chore`.
- **Worktree:** all work happens in `/tmp/claude-worktrees/public-notes-visibility` on branch `feat/public-notes-visibility`.
- **Frontend untouched.** V2 (SvelteKit page) is a separate plan.

---

## Task 1 — Schema migration

**Files:**

- Create: `projects/monolith/chart/migrations/20260507000000_knowledge_notes_visibility.sql`
- Modify (auto): `projects/monolith/chart/migrations/atlas.sum` (pre-commit hook regenerates)

**Step 1: Write the migration**

Create `projects/monolith/chart/migrations/20260507000000_knowledge_notes_visibility.sql`:

```sql
-- Add visibility label so notes can be selectively exposed via the
-- /api/knowledge/public/* surface. NULL is treated as private at
-- serving time; an explicit value is required to expose a note.
ALTER TABLE knowledge.notes
  ADD COLUMN visibility text NULL;

ALTER TABLE knowledge.notes
  ADD CONSTRAINT notes_visibility_chk
  CHECK (visibility IS NULL OR visibility IN ('public', 'private'));

-- Partial index: public set is the small, hot read path; private/null
-- reads do not need the index.
CREATE INDEX notes_visibility_idx
  ON knowledge.notes (visibility)
  WHERE visibility = 'public';
```

**Step 2: Run formatter (regenerates `atlas.sum`)**

```bash
format
```

The pre-commit hook `Update Atlas migration checksums` will refresh `atlas.sum`. If the hook is not configured, `atlas migrate hash` does the same.

**Step 3: Commit**

```bash
git add projects/monolith/chart/migrations/20260507000000_knowledge_notes_visibility.sql \
        projects/monolith/chart/migrations/atlas.sum
git commit -m "feat(knowledge): add visibility column to notes"
```

---

## Task 2 — Note model: add `Visibility` literal + `visibility` field

**Files:**

- Modify: `projects/monolith/knowledge/models.py:30-44` (add Literal next to `GapClass`)
- Modify: `projects/monolith/knowledge/models.py:54-75` (add field to `Note`)
- Test: `projects/monolith/knowledge/models_test.py` (extend)

**Step 1: Write the failing test**

Add to `projects/monolith/knowledge/models_test.py`:

```python
def test_note_visibility_accepts_public_private_or_none():
    Note(note_id="a", path="a.md", title="A", content_hash="h", visibility="public")
    Note(note_id="b", path="b.md", title="B", content_hash="h", visibility="private")
    Note(note_id="c", path="c.md", title="C", content_hash="h", visibility=None)


def test_note_visibility_rejected_at_db_constraint(db_session):
    """The Literal is type-only; the SQL CHECK is what enforces at write time.

    Insert a bogus value via raw SQL and assert the constraint fires.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                "INSERT INTO knowledge.notes "
                "(note_id, path, title, content_hash, visibility) "
                "VALUES (:id, :p, :t, :h, :v)"
            ),
            {"id": "x", "p": "x.md", "t": "X", "h": "h", "v": "yellow"},
        )
        db_session.flush()
```

**Step 2: Modify the model**

In `projects/monolith/knowledge/models.py`, after `GapState = Literal[...]`:

```python
# Mirror of the CHECK constraint in
# chart/migrations/20260507000000_knowledge_notes_visibility.sql - keep in sync.
Visibility = Literal["public", "private"]
```

In the `Note` class definition, after the `status` field:

```python
    visibility: Visibility | None = Field(
        default=None, sa_column=Column(String, nullable=True)
    )
```

**Step 3: Commit**

```bash
git add projects/monolith/knowledge/models.py projects/monolith/knowledge/models_test.py
git commit -m "feat(knowledge): add visibility field to Note model"
```

---

## Task 3 — `visibility.py` helper module

**Files:**

- Create: `projects/monolith/knowledge/visibility.py`
- Create: `projects/monolith/knowledge/visibility_test.py`

This module is the single source of truth for the criteria prompt, the coalescing rule, the wikilink sanitizer, and the SQL filter. Public routes call only into this — no scattered `if visibility == 'public'` checks.

**Step 1: Write the failing tests**

Create `projects/monolith/knowledge/visibility_test.py`:

```python
"""Tests for the visibility helper: coalescing, sanitisation, SQL filter."""

from __future__ import annotations

import pytest

from knowledge.visibility import (
    VISIBILITY_CRITERIA,
    effective_visibility,
    sanitize_public_body,
)


class _StubNote:
    def __init__(self, visibility: str | None) -> None:
        self.visibility = visibility


def test_effective_visibility_public():
    assert effective_visibility(_StubNote("public")) == "public"


def test_effective_visibility_private():
    assert effective_visibility(_StubNote("private")) == "private"


def test_effective_visibility_null_defaults_private():
    assert effective_visibility(_StubNote(None)) == "private"


def test_effective_visibility_unknown_defaults_private():
    """Defensive: a leaked-through bad value still defaults to private."""
    assert effective_visibility(_StubNote("yellow")) == "private"


def test_sanitize_no_links_passthrough():
    body = "Plain text with no wikilinks."
    assert sanitize_public_body(body, private_target_ids=set()) == body


def test_sanitize_link_to_public_kept():
    body = "See [[dora-metrics]] for the four key metrics."
    assert sanitize_public_body(body, private_target_ids=set()) == body


def test_sanitize_link_to_private_stripped_to_text():
    body = "Discussed with [[Some Colleague]] yesterday."
    out = sanitize_public_body(body, private_target_ids={"some-colleague"})
    assert out == "Discussed with Some Colleague yesterday."


def test_sanitize_link_to_unresolved_gap_stripped_to_text():
    """Unresolved targets (gaps) are treated as private — no link."""
    body = "The [[Mystery Concept]] is still being researched."
    out = sanitize_public_body(
        body, private_target_ids={"mystery-concept"}
    )
    assert out == "The Mystery Concept is still being researched."


def test_sanitize_multiple_links_per_line():
    body = "Both [[dora-metrics]] and [[Private Topic]] matter."
    out = sanitize_public_body(body, private_target_ids={"private-topic"})
    assert out == "Both [[dora-metrics]] and Private Topic matter."


def test_sanitize_malformed_brackets_left_alone():
    """We only strip well-formed [[X]] — partial brackets are not links."""
    body = "[ [not a link] ] and [[ok]] and [unfinished"
    out = sanitize_public_body(body, private_target_ids=set())
    assert "[[ok]]" in out
    assert "[unfinished" in out


def test_criteria_text_present():
    """Smoke check — every prompt that imports this string must keep it."""
    assert "visibility: public" in VISIBILITY_CRITERIA
    assert "When in doubt: `private`." in VISIBILITY_CRITERIA
```

Add a `py_test` entry for `visibility_test` in `projects/monolith/BUILD` (gazelle/`format` will normally do this, but visibility_test depends on `:monolith_backend` so confirm after running `format`).

**Step 2: Implement `visibility.py`**

Create `projects/monolith/knowledge/visibility.py`:

```python
"""Visibility helper: criteria, coalescing rule, wikilink sanitiser, SQL filter.

All knowledge-pipeline code that needs to reason about public vs private
content imports from here. Drift between the criteria the LLM sees and
the criteria the routes enforce is the most common way these systems leak.
"""

from __future__ import annotations

import re
from typing import Iterable, Literal

from sqlalchemy import or_
from sqlalchemy.sql import ColumnElement

from knowledge.models import Note

VISIBILITY_CRITERIA = """\
## Visibility (REQUIRED frontmatter field)

Every note MUST set `visibility: public` or `visibility: private`.
This controls whether the note appears on Joe's public website.

Default to `private` whenever you are uncertain.

Mark `public` when the note is about:
- General engineering concepts, principles, heuristics (DORA, Conway's Law,
  blameless postmortems, etc.) — anything you'd find in a textbook, blog,
  or conference talk.
- Skills, technologies, or methods covered in Joe's public CV / GitHub /
  conference talks.
- Verifiable facts about external systems, libraries, protocols, or tools.
- Book / paper / talk summaries when the source is publicly available.

Mark `private` when the note involves any of:
- Names of current or former colleagues, managers, reports, or interviewers.
- Specific employers in non-public ways: project codenames, internal
  architecture, compensation, performance reviews, hiring decisions.
- Job-search activity: interview prep, comp negotiation, target companies,
  reasons-for-leaving, offer comparisons.
- Personal life: family, finances, health, relationships, legal matters,
  living situation.
- Critiques or hot takes about identifiable people or companies that
  aren't already in Joe's public writing.
- Active tasks, daily/weekly journals, blockers — anything operational
  about Joe's current work.

Edge cases:
- An atom about a generally-applicable pattern that includes a
  workplace-specific example: rewrite the example out and mark public,
  OR keep the example and mark private. Do not mark public with the
  example intact.
- A fact about an external library mentioned during a private incident:
  the fact is public, the incident framing is private — split into two
  notes if needed.

When in doubt: `private`.
"""

EffectiveVisibility = Literal["public", "private"]

_VALID = {"public", "private"}

# A wikilink is a [[target]] possibly containing a |alias suffix that
# we ignore for resolution. We resolve to a slug by lowercasing and
# normalising spaces — same convention as gardener / gap_classifier.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]\n|]+)(?:\|[^\[\]\n]+)?\]\]")
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def effective_visibility(note: Note | object) -> EffectiveVisibility:
    """Return the public-or-private decision for a note.

    Null and unknown values both fall to ``private`` so a misconfigured
    or malformed value never accidentally exposes content.
    """
    raw = getattr(note, "visibility", None)
    if raw in _VALID:
        return raw  # type: ignore[return-value]
    return "private"


def _slugify(text: str) -> str:
    return _SLUG_RE.sub("-", text.strip().lower()).strip("-")


def sanitize_public_body(body: str, private_target_ids: Iterable[str]) -> str:
    """Strip every wikilink whose target is in ``private_target_ids``.

    Strip = replace ``[[Foo Bar]]`` with ``Foo Bar``. Bracket text is not
    redacted — the design accepts that wikilink display text may contain
    weak signals (the loud leak is the *graph edge*, which the public
    graph endpoint already filters out).

    Wikilinks whose target is NOT in ``private_target_ids`` are left
    untouched so the frontend renderer can still resolve them to public
    notes.
    """
    private = {p for p in private_target_ids}

    def _replace(match: re.Match[str]) -> str:
        display = match.group(1)
        slug = _slugify(display)
        if slug in private:
            return display
        return match.group(0)

    return _WIKILINK_RE.sub(_replace, body)


def public_notes_filter() -> ColumnElement[bool]:
    """SQLAlchemy clause selecting only public notes.

    Use ``Note.visibility == 'public'`` rather than ``COALESCE(..., 'private')``
    so the partial index ``notes_visibility_idx`` is selected by the planner.
    """
    return Note.visibility == "public"
```

**Step 3: Run formatter to update BUILD**

```bash
format
```

`format` runs gazelle, which adds a `py_test` entry for `visibility_test` to `projects/monolith/BUILD`. Confirm `:monolith_backend` is in its `deps` list — gazelle uses Python imports to wire deps; the test imports from `knowledge`, which is part of the bundled `:monolith_backend` library glob.

**Step 4: Commit**

```bash
git add projects/monolith/knowledge/visibility.py \
        projects/monolith/knowledge/visibility_test.py \
        projects/monolith/BUILD
git commit -m "feat(knowledge): add visibility helper module"
```

---

## Task 4 — Frontmatter parser: promote `visibility`

**Files:**

- Modify: `projects/monolith/knowledge/frontmatter.py:56-67` (add to `_PROMOTED_KEYS`)
- Modify: `projects/monolith/knowledge/frontmatter.py:82-94` (add to `ParsedFrontmatter`)
- Modify: `projects/monolith/knowledge/frontmatter.py:133-146` (project in `_build`)
- Test: `projects/monolith/knowledge/frontmatter_test.py` (extend)

**Step 1: Write failing tests**

Add to `frontmatter_test.py`:

```python
def test_visibility_public_parsed():
    raw = "---\nid: a\ntitle: A\nvisibility: public\n---\nbody"
    meta, body = frontmatter.parse(raw)
    assert meta.visibility == "public"


def test_visibility_private_parsed():
    raw = "---\nid: a\ntitle: A\nvisibility: private\n---\nbody"
    meta, _ = frontmatter.parse(raw)
    assert meta.visibility == "private"


def test_visibility_missing_is_none():
    raw = "---\nid: a\ntitle: A\n---\nbody"
    meta, _ = frontmatter.parse(raw)
    assert meta.visibility is None


def test_visibility_empty_string_is_none():
    raw = "---\nid: a\ntitle: A\nvisibility:\n---\nbody"
    meta, _ = frontmatter.parse(raw)
    assert meta.visibility is None


def test_visibility_unknown_value_warns_and_nulls(caplog):
    """Bad classifier output must not break ingest — warn, treat as null."""
    raw = "---\nid: a\ntitle: A\nvisibility: yellow\n---\nbody"
    with caplog.at_level("WARNING", logger="monolith.knowledge.frontmatter"):
        meta, _ = frontmatter.parse(raw)
    assert meta.visibility is None
    assert any("visibility" in r.message.lower() for r in caplog.records)
```

**Step 2: Implement parser changes**

In `projects/monolith/knowledge/frontmatter.py`:

1. Add `"visibility"` to `_PROMOTED_KEYS` (the set at line 56).
2. Add to `ParsedFrontmatter` (at line 82):

```python
    visibility: str | None = None
```

3. In `_build` (line 133), after `meta.status = ...`:

```python
    raw_visibility = _str_or_none(data.get("visibility"))
    if raw_visibility in (None, "public", "private"):
        meta.visibility = raw_visibility
    else:
        logger.warning(
            "frontmatter visibility has unknown value %r — treating as null",
            raw_visibility,
        )
        meta.visibility = None
```

**Step 3: Commit**

```bash
git add projects/monolith/knowledge/frontmatter.py \
        projects/monolith/knowledge/frontmatter_test.py
git commit -m "feat(knowledge): promote visibility into frontmatter parser"
```

---

## Task 5 — Reconciler / store: persist `visibility` column

**Files:**

- Modify: `projects/monolith/knowledge/store.py` (the upsert path that writes `status` from `ParsedFrontmatter`)
- Test: `projects/monolith/knowledge/store_test.py` (extend)

**Step 1: Locate the upsert path**

Find every place `status=parsed.status` (or equivalent) is passed into `Note(...)` or an UPDATE. There is at least one such path in `store.py` (note creation/update from frontmatter). Treat `visibility` identically.

**Step 2: Write failing tests**

Add to `store_test.py`:

```python
def test_upsert_writes_visibility_public(db_session):
    parsed = ParsedFrontmatter(note_id="vis-pub", title="VP", visibility="public")
    upsert_note_from_frontmatter(db_session, path="vp.md",
                                 content_hash="h", parsed=parsed, body="x")
    note = db_session.exec(select(Note).where(Note.note_id == "vis-pub")).one()
    assert note.visibility == "public"


def test_upsert_writes_visibility_private(db_session):
    parsed = ParsedFrontmatter(note_id="vis-priv", title="VPr", visibility="private")
    upsert_note_from_frontmatter(db_session, path="vpr.md",
                                 content_hash="h", parsed=parsed, body="x")
    note = db_session.exec(select(Note).where(Note.note_id == "vis-priv")).one()
    assert note.visibility == "private"


def test_upsert_null_visibility_writes_null(db_session):
    parsed = ParsedFrontmatter(note_id="vis-null", title="VN", visibility=None)
    upsert_note_from_frontmatter(db_session, path="vn.md",
                                 content_hash="h", parsed=parsed, body="x")
    note = db_session.exec(select(Note).where(Note.note_id == "vis-null")).one()
    assert note.visibility is None
```

(Adjust function names to match the actual upsert API in `store.py`.)

**Step 3: Wire `visibility` through**

Add `visibility=parsed.visibility` to every `Note(...)` constructor and every UPDATE-path field assignment that already sets `status`. The reconcile pass must rewrite the column on every run (frontmatter is canonical).

**Step 4: Commit**

```bash
git add projects/monolith/knowledge/store.py projects/monolith/knowledge/store_test.py
git commit -m "feat(knowledge): persist visibility from frontmatter on upsert"
```

---

## Task 6 — Manual edit endpoint: round-trip test only

**Files:**

- Test: `projects/monolith/knowledge/router_test.py` (extend)

No production code changes. The `PUT /api/knowledge/notes/{id}` path passes frontmatter through the parser → store flow already covered by tasks 4-5. The test guards against future regressions where a refactor accidentally drops the field.

**Step 1: Write the round-trip test**

```python
def test_put_note_round_trips_visibility(client, db_session, vault_root):
    # Create the note with visibility: public
    seed_note(db_session, vault_root, note_id="rt", visibility="public",
              body="Round trip test.")

    # Edit body via PUT, leaving frontmatter intact
    res = client.put("/api/knowledge/notes/rt", json={"body": "edited body."})
    assert res.status_code == 200

    note = db_session.exec(select(Note).where(Note.note_id == "rt")).one()
    assert note.visibility == "public"

    # Now flip visibility via a frontmatter-rewriting PUT
    res = client.put("/api/knowledge/notes/rt", json={
        "body": "edited body.",
        "frontmatter": {"visibility": "private"},
    })
    assert res.status_code == 200

    note = db_session.exec(select(Note).where(Note.note_id == "rt")).one()
    assert note.visibility == "private"
```

(Adjust to the actual PUT contract — read `router.py:188-251` and `notes_crud_test.py` for the existing shape.)

**Step 2: Commit**

```bash
git add projects/monolith/knowledge/router_test.py
git commit -m "test(knowledge): assert visibility round-trips through PUT note"
```

---

## Task 7 — Public graph endpoint

**Files:**

- Modify: `projects/monolith/knowledge/router.py` (add new route, reuse existing graph helpers)
- Test: `projects/monolith/knowledge/router_test.py` (extend)

**Step 1: Write failing tests**

Add to `router_test.py`:

```python
def test_public_graph_only_public_nodes(client, db_session, vault_root):
    seed_note(db_session, vault_root, note_id="pub-A", visibility="public")
    seed_note(db_session, vault_root, note_id="pub-B", visibility="public")
    seed_note(db_session, vault_root, note_id="priv-X", visibility="private")
    seed_note(db_session, vault_root, note_id="null-Y", visibility=None)
    seed_link(db_session, src="pub-A", target="pub-B")
    seed_link(db_session, src="pub-A", target="priv-X")
    seed_link(db_session, src="null-Y", target="pub-B")

    res = client.get("/api/knowledge/public/graph")
    assert res.status_code == 200
    body = res.json()

    node_ids = {n["id"] for n in body["nodes"]}
    assert node_ids == {"pub-A", "pub-B"}

    edge_pairs = {(e["source"], e["target"]) for e in body["edges"]}
    assert edge_pairs == {("pub-A", "pub-B")}


def test_public_graph_excludes_gap_stubs(client, db_session, vault_root):
    seed_note(db_session, vault_root, note_id="pub-A", visibility="public")
    seed_gap(db_session, term="UnresolvedThing", note_id="unresolved-thing")

    res = client.get("/api/knowledge/public/graph")
    body = res.json()
    assert "unresolved-thing" not in {n["id"] for n in body["nodes"]}


def test_public_graph_cache_headers(client, db_session, vault_root):
    seed_note(db_session, vault_root, note_id="pub-A", visibility="public")
    res = client.get("/api/knowledge/public/graph")
    cc = res.headers.get("cache-control", "")
    assert "s-maxage=3600" in cc
    assert "stale-while-revalidate=86400" in cc


def test_public_graph_etag_changes_when_public_set_mutates(
    client, db_session, vault_root
):
    seed_note(db_session, vault_root, note_id="pub-A", visibility="public")
    etag_a = client.get("/api/knowledge/public/graph").headers["etag"]
    seed_note(db_session, vault_root, note_id="pub-B", visibility="public")
    etag_b = client.get("/api/knowledge/public/graph").headers["etag"]
    assert etag_a != etag_b
```

**Step 2: Implement the endpoint**

Add to `router.py` near `get_graph` (line 116):

```python
@router.get("/public/graph")
def get_public_graph(
    response: Response,
    session: Session = Depends(get_db_session),
) -> dict:
    """Public-only knowledge graph: only public nodes, only doubly-public edges."""
    nodes_q = select(Note).where(public_notes_filter())
    public_notes = session.exec(nodes_q).all()

    public_ids = {n.note_id for n in public_notes}
    if not public_ids:
        nodes, edges = [], []
        indexed_at = None
    else:
        # Reuse existing serialiser; filter edges to doubly-public.
        nodes = [_serialize_node(n) for n in public_notes]
        link_q = (
            select(NoteLink)
            .join(Note, Note.id == NoteLink.src_note_fk)
            .where(public_notes_filter())
            .where(NoteLink.target_id.in_(public_ids))
        )
        edges = [_serialize_edge(l) for l in session.exec(link_q).all()]
        indexed_at = max((n.indexed_at for n in public_notes), default=None)

    response.headers["cache-control"] = _GRAPH_CACHE_CONTROL
    response.headers["etag"] = _graph_etag(len(public_notes), indexed_at)
    if indexed_at is not None:
        response.headers["last-modified"] = _as_utc(indexed_at).strftime(
            "%a, %d %b %Y %H:%M:%S GMT"
        )
    logger.info(
        "public.graph.served nodes=%d edges=%d", len(nodes), len(edges)
    )
    return {"nodes": nodes, "edges": edges}
```

Reuse the **existing** serialisers (`_serialize_node`, `_serialize_edge`, or whatever `get_graph` already uses) — do not duplicate. Read `router.py:116-145` and copy the same shape minus the visibility filter.

Import `from knowledge.visibility import public_notes_filter`.

**Step 3: Commit**

```bash
git add projects/monolith/knowledge/router.py projects/monolith/knowledge/router_test.py
git commit -m "feat(knowledge): add GET /api/knowledge/public/graph"
```

---

## Task 8 — Public note endpoint

**Files:**

- Modify: `projects/monolith/knowledge/router.py` (add `GET /public/notes/{note_id}`)
- Test: `projects/monolith/knowledge/router_test.py` (extend)

**Step 1: Write failing tests**

```python
def test_public_note_200_for_public(client, db_session, vault_root):
    seed_note(db_session, vault_root, note_id="pub-A", visibility="public",
              body="Hello [[pub-B]] world.")
    seed_note(db_session, vault_root, note_id="pub-B", visibility="public")
    res = client.get("/api/knowledge/public/notes/pub-A")
    assert res.status_code == 200
    body = res.json()
    # Public-target wikilink left intact for frontend resolution
    assert "[[pub-B]]" in body["body"]


def test_public_note_404_for_missing(client):
    res = client.get("/api/knowledge/public/notes/does-not-exist")
    assert res.status_code == 404


def test_public_note_404_for_private_same_response_shape(
    client, db_session, vault_root
):
    seed_note(db_session, vault_root, note_id="priv-X", visibility="private")
    res_priv = client.get("/api/knowledge/public/notes/priv-X")
    res_miss = client.get("/api/knowledge/public/notes/does-not-exist")
    assert res_priv.status_code == 404
    assert res_miss.status_code == 404
    assert res_priv.json() == res_miss.json()


def test_public_note_strips_private_wikilinks_from_body(
    client, db_session, vault_root
):
    seed_note(db_session, vault_root, note_id="pub-A", visibility="public",
              body="See [[pub-B]] and avoid [[Some Colleague]].")
    seed_note(db_session, vault_root, note_id="pub-B", visibility="public")
    seed_note(db_session, vault_root, note_id="some-colleague",
              visibility="private")

    res = client.get("/api/knowledge/public/notes/pub-A")
    assert res.status_code == 200
    body = res.json()["body"]
    assert "[[pub-B]]" in body
    assert "[[Some Colleague]]" not in body
    assert "Some Colleague" in body  # bracket text is preserved


def test_public_note_response_excludes_internal_fields(
    client, db_session, vault_root
):
    seed_note(db_session, vault_root, note_id="pub-A", visibility="public",
              extra={"internal_only": "secret"})
    res = client.get("/api/knowledge/public/notes/pub-A")
    body = res.json()
    assert "extra" not in body
    assert "secret" not in str(body)
```

**Step 2: Implement the endpoint**

Add to `router.py`:

```python
@router.get("/public/notes/{note_id}")
def get_public_note(
    note_id: str,
    session: Session = Depends(get_db_session),
) -> dict:
    note = session.exec(select(Note).where(Note.note_id == note_id)).one_or_none()
    if note is None or effective_visibility(note) != "public":
        # Identical 404 for missing and private — never expose existence.
        reason = "not_found" if note is None else "private_gated"
        logger.info("public.note.404 note_id=%s reason=%s", note_id, reason)
        raise HTTPException(status_code=404, detail="Not Found")

    private_ids = {
        n.note_id
        for n in session.exec(
            select(Note).where(
                or_(Note.visibility != "public", Note.visibility.is_(None))
            )
        ).all()
    }

    body = sanitize_public_body(_load_note_body(note), private_ids)
    logger.info("public.note.served note_id=%s", note_id)
    return {
        "note_id": note.note_id,
        "title": note.title,
        "tags": list(note.tags),
        "aliases": list(note.aliases),
        "indexed_at": _as_utc(note.indexed_at).isoformat()
            if note.indexed_at else None,
        "body": body,
    }
```

Imports:

```python
from knowledge.visibility import effective_visibility, sanitize_public_body
from sqlalchemy import or_
```

**Step 3: Commit**

```bash
git add projects/monolith/knowledge/router.py projects/monolith/knowledge/router_test.py
git commit -m "feat(knowledge): add GET /api/knowledge/public/notes/{id}"
```

---

## Task 9 — Gardener atom prompt: append visibility criteria

**Files:**

- Modify: `projects/monolith/knowledge/gardener.py:31-144` (`_CLAUDE_PROMPT_HEADER`)
- Test: extend an existing `gardener_test.py` or `mcp_test.py`

**Step 1: Write the failing test**

```python
def test_gardener_prompt_includes_visibility_criteria():
    from knowledge.gardener import _CLAUDE_PROMPT_HEADER
    from knowledge.visibility import VISIBILITY_CRITERIA
    rendered = _CLAUDE_PROMPT_HEADER.format(
        raw_id="r", raw_file_path="x", processed_root="p", title="t"
    )
    assert "visibility: public|private" in rendered
    # criteria must be appended verbatim — drift = leak
    assert VISIBILITY_CRITERIA.strip() in rendered
```

**Step 2: Modify the prompt**

Two changes to `_CLAUDE_PROMPT_HEADER` (line 31):

1. In the frontmatter template (line 60-69), add the `visibility` line **before** `tags`:

```
visibility: public|private   # REQUIRED — see criteria below
```

2. Append `\n\n` + `VISIBILITY_CRITERIA` to the prompt body (just before the trailing `Title: {title}`).

Implementation: introduce a module-level format-string constant rather than inlining, so the substitution is mechanical:

```python
from knowledge.visibility import VISIBILITY_CRITERIA

_CLAUDE_PROMPT_HEADER = """\
... existing content ...

{VISIBILITY_CRITERIA}

Title: {title}

""".replace("{VISIBILITY_CRITERIA}", VISIBILITY_CRITERIA)
```

Or — simpler and more explicit — manually paste the criteria block into the prompt, but assert by import in tests so any future drift is caught.

**Step 3: Commit**

```bash
git add projects/monolith/knowledge/gardener.py projects/monolith/knowledge/gardener_test.py
git commit -m "feat(knowledge): require visibility in gardener atom prompt"
```

---

## Task 10 — Gardener distill prompt: append visibility criteria

**Files:**

- Modify: `projects/monolith/knowledge/gardener.py:146-178` (`_DISTILL_PROMPT`)
- Test: extend `gardener_distill_test.py`

**Step 1: Write the failing test**

```python
def test_distill_prompt_includes_visibility_criteria():
    from knowledge.gardener import _DISTILL_PROMPT
    from knowledge.visibility import VISIBILITY_CRITERIA
    rendered = _DISTILL_PROMPT.format(note_id="n", note_path="x",
                                      processed_root="p", title="t")
    assert "visibility:" in rendered
    assert VISIBILITY_CRITERIA.strip() in rendered
```

**Step 2: Modify `_DISTILL_PROMPT`**

Add `visibility: public|private` to the required-fields list in step 5 (line 157-159), and append `VISIBILITY_CRITERIA` before the trailing `Title: {title}`.

**Step 3: Commit**

```bash
git add projects/monolith/knowledge/gardener.py \
        projects/monolith/knowledge/gardener_distill_test.py
git commit -m "feat(knowledge): require visibility in gardener distill prompt"
```

---

## Task 11 — Research writer: set `visibility: public`

**Files:**

- Modify: `projects/monolith/knowledge/research_writer.py:61-71` (the `fm` dict in `write_research_raw`)
- Test: `projects/monolith/knowledge/research_writer_test.py`

**Step 1: Write the failing test**

```python
def test_research_raw_sets_visibility_public(tmp_path):
    out_path = write_research_raw(
        vault_root=tmp_path, slug="dora-metrics",
        title="DORA Metrics", summary="...",
        supported_claims=[], sources=[],
        agent_model="m", researched_at="2026-05-07",
    )
    text = out_path.read_text()
    assert "visibility: public" in text


def test_failed_research_does_not_set_visibility(tmp_path):
    """Quarantined drafts are forensic; visibility is irrelevant."""
    out_path = quarantine(
        vault_root=tmp_path, slug="bad", attempt=1, summary="...",
        pre_filter_claims=[], sources=[],
        agent_model="m", researched_at="2026-05-07",
    )
    assert "visibility" not in out_path.read_text()
```

**Step 2: Modify `write_research_raw`**

Add to the `fm` dict (line 61):

```python
        "visibility": "public",
```

Place it after `"type": "research"` so the order in the resulting YAML stays readable.

Do not modify `quarantine` — failed_research notes never reach the public surface.

**Step 3: Commit**

```bash
git add projects/monolith/knowledge/research_writer.py \
        projects/monolith/knowledge/research_writer_test.py
git commit -m "feat(knowledge): mark research raws visibility: public"
```

---

## Task 12 — Phase-1 script: `add_visibility_field`

**Files:**

- Create: `projects/monolith/knowledge/tools/add_visibility_field.py`
- Create: `projects/monolith/knowledge/tools/add_visibility_field_test.py`

**Step 1: Write failing tests**

```python
"""Tests for the mechanical Phase-1 visibility field injector."""

from pathlib import Path

from knowledge.tools.add_visibility_field import run


def _write(p: Path, body: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


def test_inserts_visibility_into_files_lacking_it(tmp_path):
    note = tmp_path / "_processed/foo.md"
    _write(note, "---\nid: foo\ntitle: Foo\n---\nbody\n")
    stats = run(vault_root=tmp_path, dirs=["_processed"])
    assert stats.added == 1
    assert "visibility:\n" in note.read_text()


def test_skips_files_already_with_visibility(tmp_path):
    note = tmp_path / "_processed/bar.md"
    _write(note, "---\nid: bar\ntitle: Bar\nvisibility: public\n---\nbody\n")
    before = note.read_text()
    stats = run(vault_root=tmp_path, dirs=["_processed"])
    assert stats.added == 0
    assert stats.already_set == 1
    assert note.read_text() == before  # byte-stable


def test_idempotent_second_run(tmp_path):
    note = tmp_path / "_processed/baz.md"
    _write(note, "---\nid: baz\ntitle: Baz\n---\nbody\n")
    run(vault_root=tmp_path, dirs=["_processed"])
    after_first = note.read_text()
    run(vault_root=tmp_path, dirs=["_processed"])
    assert note.read_text() == after_first


def test_skips_files_with_no_frontmatter(tmp_path):
    note = tmp_path / "_processed/raw.md"
    _write(note, "Just a body, no frontmatter.\n")
    stats = run(vault_root=tmp_path, dirs=["_processed"])
    assert stats.added == 0
    assert stats.parse_skipped == 1


def test_skips_files_with_unparseable_frontmatter(tmp_path):
    note = tmp_path / "_processed/broken.md"
    _write(note, "---\nthis: is: not: yaml\n---\nbody\n")
    stats = run(vault_root=tmp_path, dirs=["_processed"])
    assert stats.parse_skipped == 1


def test_atomic_write_does_not_truncate_on_failure(tmp_path, monkeypatch):
    note = tmp_path / "_processed/atomic.md"
    _write(note, "---\nid: a\n---\nbody\n")

    def boom(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr("os.replace", boom)
    try:
        run(vault_root=tmp_path, dirs=["_processed"])
    except OSError:
        pass
    # Original file is intact
    assert "id: a" in note.read_text()
```

**Step 2: Implement the script**

```python
"""Phase-1 mechanical injection of `visibility:` into existing note frontmatter.

Idempotent. No LLM calls. No DB writes. Walks the configured vault dirs,
inserts `visibility:` (empty value) into every .md file that lacks the
key. The resulting frontmatter is parsed by the reconciler on its next
pass and persists null in the DB column — which the serving layer
treats as private.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from knowledge import frontmatter

logger = logging.getLogger("monolith.knowledge.tools.add_visibility_field")

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


@dataclass
class Stats:
    added: int = 0
    already_set: int = 0
    parse_skipped: int = 0


def _insert_visibility_line(block: str) -> str:
    """Insert `visibility:` after `id:` (or at the top if no id)."""
    lines = block.splitlines()
    insert_at = 0
    for i, line in enumerate(lines):
        if line.startswith("id:"):
            insert_at = i + 1
            break
    lines.insert(insert_at, "visibility:")
    return "\n".join(lines)


def _process_file(path: Path) -> str | None:
    """Return the new file content, or None if no change is needed."""
    raw = path.read_text()
    match = _FRONTMATTER_RE.match(raw)
    if not match:
        return "PARSE_SKIP"

    block = match.group(1)
    try:
        meta, _ = frontmatter.parse(raw)
    except frontmatter.FrontmatterError as exc:
        logger.warning("parse failure for %s: %s — skipping", path, exc)
        return "PARSE_SKIP"

    if "visibility" in {line.split(":", 1)[0].strip() for line in block.splitlines()}:
        return None

    new_block = _insert_visibility_line(block)
    return raw[: match.start()] + f"---\n{new_block}\n---\n" + raw[match.end() :]


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def run(*, vault_root: Path, dirs: list[str]) -> Stats:
    stats = Stats()
    for d in dirs:
        for md in (vault_root / d).rglob("*.md"):
            outcome = _process_file(md)
            if outcome == "PARSE_SKIP":
                stats.parse_skipped += 1
                continue
            if outcome is None:
                stats.already_set += 1
                continue
            _atomic_write(md, outcome)
            stats.added += 1
    return stats


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument("--dirs", nargs="+", default=["_processed"])
    args = parser.parse_args()
    stats = run(vault_root=args.vault_root, dirs=args.dirs)
    print(f"added={stats.added} already_set={stats.already_set} "
          f"parse_skipped={stats.parse_skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

The script lives under `projects/monolith/knowledge/tools/`. Gazelle (via `format`) wires it into the `:monolith_backend` glob automatically since the parent BUILD globs `knowledge/**/*.py`. Wire it as a `py_venv_binary` in `projects/monolith/BUILD` if a separate `bazel run` target is desired — model on the existing `:main` binary at the top of that BUILD.

**Step 3: Run formatter**

```bash
format
```

**Step 4: Commit**

```bash
git add projects/monolith/knowledge/tools/add_visibility_field.py \
        projects/monolith/knowledge/tools/add_visibility_field_test.py \
        projects/monolith/BUILD
git commit -m "feat(knowledge): add Phase-1 visibility field injector"
```

---

## Task 13 — Phase-2 script: `classify_visibility_backfill`

**Files:**

- Create: `projects/monolith/knowledge/tools/classify_visibility_backfill.py`
- Create: `projects/monolith/knowledge/tools/classify_visibility_backfill_test.py`

The script shells out to the `claude` CLI (per `memory/feedback_claude_cli_subprocess_for_tos.md`). Tests mock the subprocess — never call the real CLI.

**Step 1: Write failing tests**

```python
"""Tests for the LLM-driven Phase-2 visibility backfill."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from knowledge.tools.classify_visibility_backfill import (
    BackfillError,
    classify_one,
    run,
)


def _seed_note(p: Path, slug: str, visibility: str | None = None) -> Path:
    fm = f"---\nid: {slug}\ntitle: {slug}\n"
    if visibility is not None:
        fm += f"visibility: {visibility}\n"
    else:
        fm += "visibility:\n"
    fm += "---\nbody for {slug}.\n"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(fm.format(slug=slug))
    return p


@pytest.fixture
def vault(tmp_path):
    _seed_note(tmp_path / "_processed/a.md", "a", visibility=None)
    _seed_note(tmp_path / "_processed/b.md", "b", visibility="public")
    _seed_note(tmp_path / "_processed/c.md", "c", visibility=None)
    return tmp_path


def test_refuses_report_path_inside_repo(vault):
    with pytest.raises(BackfillError, match="outside the repo"):
        run(vault_root=vault, report=vault / "out.json", batch_size=1,
            run_one_for_test=lambda body: ("private", "test"))


def test_skips_already_labelled_notes(vault):
    calls = []

    def fake(body):
        calls.append(body)
        return "public", "ok"

    run(vault_root=vault, report=Path("/tmp/r.json"), batch_size=2,
        run_one_for_test=fake)
    # b.md is already "public" — skip; only a + c go through classifier
    assert len(calls) == 2


def test_writes_decision_back_to_frontmatter(vault):
    def fake(body):
        return "public", "looks public"

    run(vault_root=vault, report=Path("/tmp/r.json"), batch_size=1,
        run_one_for_test=fake)
    a = (vault / "_processed/a.md").read_text()
    assert "visibility: public" in a


def test_strict_json_parse_failure_leaves_file_unchanged(vault, tmp_path):
    def bad(body):
        raise BackfillError("invalid JSON")

    run(vault_root=vault, report=Path("/tmp/r.json"), batch_size=1,
        run_one_for_test=bad)
    # a.md still has visibility: (empty)
    a = (vault / "_processed/a.md").read_text()
    assert "visibility:\n" in a or "visibility: \n" in a


def test_resumable_after_partial_run(vault):
    # First run: only handle a (a → public)
    def fake_a(body):
        return "public", "ok"

    run(vault_root=vault, report=Path("/tmp/r.json"), batch_size=1,
        run_one_for_test=fake_a, max_files=1)

    # Second run: c is the only remaining unlabelled
    seen = []
    def fake_c(body):
        seen.append(body)
        return "private", "ok"
    run(vault_root=vault, report=Path("/tmp/r.json"), batch_size=1,
        run_one_for_test=fake_c)
    assert len(seen) == 1


@patch("subprocess.run")
def test_classify_one_parses_strict_json(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=json.dumps({"visibility": "public", "rationale": "concept"}),
        stderr="",
    )
    decision, rationale = classify_one(body="body", title="t")
    assert decision == "public"
    assert rationale == "concept"


@patch("subprocess.run")
def test_classify_one_rejects_unknown_decision(mock_run):
    mock_run.return_value = subprocess.CompletedProcess(
        args=[], returncode=0,
        stdout=json.dumps({"visibility": "yellow", "rationale": "x"}),
        stderr="",
    )
    with pytest.raises(BackfillError):
        classify_one(body="body", title="t")
```

**Step 2: Implement the script**

```python
"""Phase-2 backfill: classify every unlabelled note via the `claude` CLI.

The script writes ``visibility: public`` or ``visibility: private`` into
each note's frontmatter via in-place line replacement (no full YAML
round-trip). Per `memory/feedback_claude_cli_subprocess_for_tos.md`, we
shell out to the `claude` CLI rather than the Anthropic SDK.

Resumable: re-running picks up only still-unlabelled notes. Crash
recovery is implicit — partial work is on disk.

Report: a JSON file written OUTSIDE the repo (the script refuses
report paths inside the working tree). Rationales quote note content
and must not be committed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from knowledge import frontmatter
from knowledge.visibility import VISIBILITY_CRITERIA

logger = logging.getLogger(
    "monolith.knowledge.tools.classify_visibility_backfill"
)

_FRONTMATTER_RE = re.compile(r"^(---\n)(.*?)(\n---\n)", re.DOTALL)
_VISIBILITY_LINE_RE = re.compile(r"^visibility:.*$", re.MULTILINE)


class BackfillError(Exception):
    """Raised when the classifier output cannot be safely applied."""


@dataclass
class _ReportEntry:
    note_id: str
    decision: str | None
    rationale: str | None
    error: str | None = None


@dataclass
class _Report:
    entries: list[_ReportEntry] = field(default_factory=list)


def _check_report_path_outside_repo(report: Path) -> None:
    cwd = Path.cwd().resolve()
    try:
        report.resolve().relative_to(cwd)
    except ValueError:
        return  # outside cwd — good
    raise BackfillError(
        f"refusing to write report at {report} — must be outside the repo "
        "working tree (rationales quote note content; never commit)"
    )


def classify_one(*, body: str, title: str) -> tuple[str, str]:
    """Single LLM call via the `claude` CLI subprocess. Returns (decision, rationale)."""
    prompt = (
        f"{VISIBILITY_CRITERIA}\n\n"
        f"Title: {title}\n\nBody:\n{body}\n\n"
        "Respond with strict JSON:\n"
        '{"visibility": "public" | "private", "rationale": "<one sentence>"}\n'
    )
    proc = subprocess.run(
        ["claude", "--print", "--output-format", "json"],
        input=prompt, capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise BackfillError(
            f"claude exit={proc.returncode} stderr={proc.stderr[:200]!r}"
        )
    try:
        # `claude --output-format json` wraps the response; the actual
        # answer is in the .result field. Adjust if the CLI shape differs.
        outer = json.loads(proc.stdout)
        text = outer.get("result", proc.stdout)
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BackfillError(f"non-JSON output: {exc}") from exc

    decision = parsed.get("visibility")
    rationale = parsed.get("rationale", "")
    if decision not in {"public", "private"}:
        raise BackfillError(f"invalid decision {decision!r}")
    return decision, str(rationale)


def _replace_visibility_line(content: str, value: str) -> str:
    match = _FRONTMATTER_RE.match(content)
    if not match:
        raise BackfillError("note has no frontmatter")
    head, block, tail = match.group(1), match.group(2), match.group(3)
    if _VISIBILITY_LINE_RE.search(block):
        new_block = _VISIBILITY_LINE_RE.sub(f"visibility: {value}", block)
    else:
        new_block = block + f"\nvisibility: {value}"
    return head + new_block + tail + content[match.end() :]


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content)
    os.replace(tmp, path)


def _is_unlabelled(meta: frontmatter.ParsedFrontmatter) -> bool:
    return meta.visibility in (None, "")


def run(
    *,
    vault_root: Path,
    report: Path,
    batch_size: int = 50,
    run_one_for_test: Callable[[str], tuple[str, str]] | None = None,
    max_files: int | None = None,
) -> _Report:
    """Walk vault and classify every unlabelled note. ``run_one_for_test``
    is a seam used by tests — production passes None and we use ``classify_one``.
    """
    _check_report_path_outside_repo(report)
    rep = _Report()

    files = sorted((vault_root / "_processed").rglob("*.md"))
    processed = 0
    for path in files:
        if max_files is not None and processed >= max_files:
            break
        raw = path.read_text()
        try:
            meta, body = frontmatter.parse(raw)
        except frontmatter.FrontmatterError:
            rep.entries.append(_ReportEntry(
                note_id=path.stem, decision=None, rationale=None,
                error="frontmatter parse failure",
            ))
            continue
        if not _is_unlabelled(meta):
            continue

        try:
            if run_one_for_test is not None:
                decision, rationale = run_one_for_test(body)
            else:
                decision, rationale = classify_one(body=body, title=meta.title or path.stem)
        except BackfillError as exc:
            rep.entries.append(_ReportEntry(
                note_id=path.stem, decision=None, rationale=None,
                error=str(exc),
            ))
            continue

        new = _replace_visibility_line(raw, decision)
        _atomic_write(path, new)
        rep.entries.append(_ReportEntry(
            note_id=path.stem, decision=decision, rationale=rationale,
        ))
        processed += 1

    report.write_text(json.dumps(
        [e.__dict__ for e in rep.entries], indent=2,
    ))
    return rep


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument(
        "--report", type=Path, required=True,
        help="must be outside the repo working tree",
    )
    parser.add_argument("--batch-size", type=int, default=50)
    args = parser.parse_args()
    rep = run(vault_root=args.vault_root, report=args.report,
              batch_size=args.batch_size)
    classified = sum(1 for e in rep.entries if e.decision)
    errored = sum(1 for e in rep.entries if e.error)
    print(f"classified={classified} errored={errored} report={args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

**Step 3: Run formatter**

```bash
format
```

**Step 4: Commit**

```bash
git add projects/monolith/knowledge/tools/classify_visibility_backfill.py \
        projects/monolith/knowledge/tools/classify_visibility_backfill_test.py \
        projects/monolith/BUILD
git commit -m "feat(knowledge): add Phase-2 visibility backfill classifier"
```

---

## Task 14 — Chart bump, observability log lines, open PR

**Files:**

- Modify: `projects/monolith/chart/Chart.yaml` (bump version)
- Modify: `projects/monolith/deploy/application.yaml` (matching `targetRevision`)

**Step 1: Bump chart version**

Per `memory/feedback_chart_version_bumps.md`. Read both files, increment patch version in both.

**Step 2: Verify observability log lines from Tasks 7-8 are wired**

Confirm:

- `public.graph.served nodes=N edges=M`
- `public.note.404 note_id=X reason={not_found|private_gated}`
- `public.note.served note_id=X`

These were added in Tasks 7-8; this step is just a final grep:

```bash
grep -n "public.graph.served\|public.note.404\|public.note.served" \
    projects/monolith/knowledge/router.py
```

All three should appear.

**Step 3: Commit**

```bash
git add projects/monolith/chart/Chart.yaml projects/monolith/deploy/application.yaml
git commit -m "chore(monolith): bump chart version for visibility schema"
```

**Step 4: Push and open PR**

```bash
git push -u origin feat/public-notes-visibility

gh pr create --title "feat(knowledge): visibility labels and public API surface (V1)" --body "$(cat <<'EOF'
## Summary

V1 of public notes — a binary `visibility: public | private` label on every
note plus a strictly filtered public API at `/api/knowledge/public/...`.
Frontend (V2) and chat (V3) are separate plans.

Frontmatter is canonical; the DB column mirrors it; null is treated as
private. The new `knowledge/visibility.py` module owns the criteria
prompt, the coalescing rule, the wikilink sanitiser, and the SQL filter.
Public routes call only into that helper.

Two vault-mutation scripts ship under `knowledge/tools/`:
- `add_visibility_field` — Phase 1, mechanical, no LLM, idempotent.
- `classify_visibility_backfill` — Phase 2, LLM via `claude` CLI, single-commit.

Design doc: `docs/plans/2026-05-07-public-notes-visibility-design.md`
Plan doc: `docs/plans/2026-05-07-public-notes-visibility-v1-plan.md`

## Test plan

- [ ] CI green
- [ ] Migration applies cleanly in CI's ephemeral DB
- [ ] `format` produced no further BUILD changes
- [ ] Public endpoints exist; private routes unchanged
- [ ] Phase-1 / Phase-2 scripts import and `--help` parses

## Rollout (post-merge)

1. Run Phase 1 script against vault; commit + push.
2. Wait for reconciler to populate the column with nulls.
3. Run Phase 2 script (single `bazel run`, single vault commit, `/tmp/`-bound report).
4. Smoke-test public endpoints.
EOF
)"
```

**Step 5: Watch CI**

```bash
gh pr checks <number> --watch
```

If failures, diagnose via `mcp__buildbuddy__get_invocation` (use `commitSha` selector) → `get_target` → `get_log`. **Quote the actual assertion error verbatim** before hypothesising — never blame infra without a real test failure first.

---

## Acceptance criteria

CI green on the PR with all of the following:

- `notes.visibility` column exists with CHECK constraint and partial index.
- `Note.visibility` field is `Visibility | None`.
- `frontmatter.parse()` recognises `visibility:` and warns-but-doesn't-fail on bad values.
- Reconciler writes the column from frontmatter on every upsert.
- `knowledge/visibility.py` exports `VISIBILITY_CRITERIA`, `effective_visibility`, `sanitize_public_body`, `public_notes_filter`.
- Gardener atom prompt + distill prompt include the criteria block; tests assert this.
- `research_writer` sets `visibility: public` on research raws.
- `GET /api/knowledge/public/graph` returns only public nodes + doubly-public edges, has correct cache headers + ETag.
- `GET /api/knowledge/public/notes/{id}` returns 200 for public, identical 404 for missing/private, body sanitised.
- Phase 1 script: idempotent, atomic, parse-failure-tolerant.
- Phase 2 script: refuses report inside repo, resumable, mocked-subprocess tests pass.
- Chart + targetRevision bumped.
