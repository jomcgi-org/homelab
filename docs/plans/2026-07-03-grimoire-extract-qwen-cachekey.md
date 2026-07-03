# Grimoire Extraction: Local Qwen + Versioned Cache Key Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Move Grimoire entity extraction onto the free in-cluster Qwen model, make re-processing idempotent under a `(chunk, model, prompt)` cache key, and add a one-shot JSON self-correction retry so cheaper models don't waste runs.

**Architecture:** The extraction job (`grimoire/extract.py`, driven by `grimoire/jobs.py` as an Argo CronWorkflow) currently calls `anthropic/claude-sonnet-4.5` via a hardcoded OpenRouter URL and treats a chunk as "done" if it has any `chunk_entity_mention` row. We (1) make the endpoint + auth env-driven so it points at the OpenAI-compatible vLLM at `http://inference.inference.svc.cluster.local:8080/v1/chat/completions` (model `qwen3.6-27b`, no API key), (2) replace the mention-existence dedup with an explicit `grimoire.chunk_extraction` marker keyed on `(chunk_id, model, prompt_hash)` that records `ok`/`empty` (but never failures, so failures retry), and (3) add a local fence-strip plus one model self-correction turn on JSON parse failure. A `GRIMOIRE_EXTRACT_BOOK` filter scopes manual staging runs to one book.

**Tech Stack:** Python (SQLModel/SQLAlchemy, httpx, asyncio), Atlas migrations (SQL in `chart/migrations/`), Bazel/`aspect_rules_py` (`py_test` in `projects/monolith/BUILD`), Helm values, BuildBuddy CI (no local test loop).

**Key references:**

- `projects/monolith/grimoire/extract.py` - `OpenRouterClient`, `_parse_content`, `_select_pending_chunks`, `extract_chunks`, `_apply_extraction`, `EXTRACTION_PROMPT`, `OPENROUTER_URL`, `DEFAULT_MODEL`.
- `projects/monolith/grimoire/models.py` - SQLModel table pattern: `_UUID` helper, `__table_args__ = {"schema": "grimoire", "extend_existing": True}`, `created_at` Field. Header says "keep in sync" with the migration.
- `projects/monolith/grimoire/jobs.py` - `grimoire_extract_entities` (the `OPENROUTER_API_KEY` skip guard lives here).
- `projects/monolith/chart/migrations/20260703070000_grimoire_schema.sql` - schema DDL to mirror.
- `projects/monolith/BUILD` - hand-added `grimoire_*_test` `py_test` targets (~line 2596+). Per repo convention a NEW `*_test.py` needs its `py_test` added by hand; Format check passes green without it, so do not rely on gazelle.
- `projects/monolith/deploy/values.yaml` - `llamaCppUrl`/`embeddingUrl` and the goosecracker `OPENAI_HOST: http://inference.inference.svc.cluster.local:8080` / `GOOSE_MODEL: qwen3.6-27b` precedent for reaching in-cluster Qwen. Workflows pods get env via `jobs.cronWorkflows` values.
- `projects/monolith/chart/Chart.yaml` + `projects/monolith/deploy/application.yaml` - version + `targetRevision` must move together.

**Gotchas to bake in:**

- `model_dump()` on a `table=True` SQLModel silently drops SQLAlchemy-expired attributes after commit - flatten via `__table__.columns.keys()` + `getattr` when you need a dict from a persisted row.
- Migration timestamp IDs can collide with concurrent PRs; the `atlas_sum_test` catches it after rebase. Number the new migration LATER than `20260703070000`, and let the Atlas pre-commit hook rehash `atlas.sum` (do not hand-edit the sum).
- SQLite test fixtures use `SQLModel.metadata.create_all` (no migrations), so mirror any CHECK constraint in `__table_args__` or SQLite won't enforce it.
- No local test loop: implement, commit, push the branch, watch `gh pr checks <n> --watch` + BuildBuddy MCP for failures. Do NOT run `bazel test` locally.
- Staged A/B quality eval is limited by global entity dedup (`(entity_type, lower(name))`, first-write-wins): a new prompt over a book a prior version already extracted reuses the old entity spine. Eval new prompts on a FRESH book id.

---

### Task 1: `chunk_extraction` marker table (model + migration)

**Files:**

- Modify: `projects/monolith/grimoire/models.py` (add `ChunkExtraction`)
- Create: `projects/monolith/chart/migrations/20260703XXXXXX_grimoire_chunk_extraction.sql` (pick a timestamp strictly greater than `20260703070000`; e.g. `20260703120000`)
- Test: `projects/monolith/grimoire/models_test.py` (extend)

**Step 1: Write the failing test**

In `models_test.py`, add a test that the model round-trips through a SQLite `create_all` fixture and rejects a bad `status`:

```python
def test_chunk_extraction_marker_roundtrip(session):  # session = in-memory SQLite fixture, create_all
    from grimoire.models import ChunkExtraction

    row = ChunkExtraction(
        chunk_id="11111111-1111-1111-1111-111111111111",
        model="qwen3.6-27b",
        prompt_hash="abc123",
        status="empty",
    )
    session.add(row)
    session.commit()
    got = session.get(
        ChunkExtraction,
        ("11111111-1111-1111-1111-111111111111", "qwen3.6-27b", "abc123"),
    )
    assert got is not None
    assert got.status == "empty"
    assert got.extracted_at is not None


def test_chunk_extraction_status_check(session):
    from sqlalchemy.exc import IntegrityError
    from grimoire.models import ChunkExtraction

    session.add(
        ChunkExtraction(
            chunk_id="22222222-2222-2222-2222-222222222222",
            model="m",
            prompt_hash="h",
            status="bogus",
        )
    )
    with pytest.raises(IntegrityError):
        session.commit()
```

Match the existing fixture/import style already in `models_test.py` (reuse its session fixture; add `import pytest` if absent).

**Step 2: Verify it fails** - `ChunkExtraction` does not exist yet (import error).

**Step 3: Add the model** in `models.py` (after `ChunkEntityMention`), mirroring the schema/`_UUID` conventions. Composite PK across the three key columns; CHECK on status mirrored so SQLite enforces it:

```python
ExtractionStatus = Literal["ok", "empty"]


class ChunkExtraction(SQLModel, table=True):
    """Processed-marker: one row per (chunk, model, prompt_hash) successfully
    extracted. Presence means "this chunk is done under this exact model +
    prompt"; absence means pending. We record status='empty' for zero-yield
    chunks (so they aren't re-run forever) but write NOTHING on HTTP/parse
    failure, so genuine failures are naturally re-selected next run. The key
    deliberately excludes chunk content hash in v1 (in-place content edits are
    out of scope; re-loading a changed chunk under a seen model+prompt will not
    re-extract until content hashing is added)."""

    __tablename__ = "chunk_extraction"
    __table_args__ = (
        CheckConstraint(
            "status IN ('ok', 'empty')",
            name="chunk_extraction_status_chk",
        ),
        {"schema": "grimoire", "extend_existing": True},
    )

    chunk_id: str = Field(
        sa_column=_uuid_column(
            primary_key=True, nullable=False, fk="grimoire.knowledge_chunk.id"
        )
    )
    model: str = Field(sa_column=Column(String, primary_key=True, nullable=False))
    prompt_hash: str = Field(sa_column=Column(String, primary_key=True, nullable=False))
    status: ExtractionStatus = Field(sa_column=Column(String, nullable=False))
    extracted_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
```

**Step 4: Write the migration** `...grimoire_chunk_extraction.sql`:

```sql
CREATE TABLE grimoire.chunk_extraction (
    chunk_id     UUID NOT NULL REFERENCES grimoire.knowledge_chunk (id) ON DELETE CASCADE,
    model        TEXT NOT NULL,
    prompt_hash  TEXT NOT NULL,
    status       TEXT NOT NULL,
    extracted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chunk_id, model, prompt_hash),
    CONSTRAINT chunk_extraction_status_chk CHECK (status IN ('ok', 'empty'))
);
```

**Step 5: Register the test + let Atlas rehash.** Ensure `grimoire_models_test` in `projects/monolith/BUILD` already covers `grimoire/models_test.py` (it does; no new target needed). Stage everything so the Atlas pre-commit hook rehashes `atlas.sum`.

**Step 6: Commit**

```bash
git add projects/monolith/grimoire/models.py \
        projects/monolith/grimoire/models_test.py \
        projects/monolith/chart/migrations/ projects/monolith/chart/migrations/atlas.sum
git commit -m "feat(grimoire): add chunk_extraction processed-marker table"
```

---

### Task 2: Select pending chunks by `(model, prompt_hash)` + book filter, and record markers

**Files:**

- Modify: `projects/monolith/grimoire/extract.py` (`_select_pending_chunks`, `extract_chunks`, add `_prompt_hash`; import `ChunkExtraction`)
- Test: `projects/monolith/grimoire/extract_test.py`

**Step 1: Write failing tests.** Replace the current "no mention row = pending" assertions with marker-based ones. Cover: (a) a chunk with a matching `(model, prompt_hash)` marker is skipped; (b) same chunk under a DIFFERENT model or prompt_hash is re-selected; (c) after a successful run a marker row exists with `status` `ok` (found entities) or `empty` (found none); (d) `GRIMOIRE_EXTRACT_BOOK` restricts selection to one `book_id`; (e) a chunk whose extraction raised is left WITHOUT a marker (so it stays pending).

```python
def test_processed_marker_skips_same_model_prompt(session, fake_or_client, fake_embedder):
    # seed a chunk + a ChunkExtraction marker for the client's model + current prompt hash
    ...
    result = asyncio.run(extract_chunks(session, fake_or_client, fake_embedder))
    assert result["chunks_processed"] == 0  # skipped by marker


def test_processed_marker_reextracts_on_model_change(session, fake_or_client_modelB, fake_embedder):
    # marker exists for modelA; client is modelB -> chunk is pending again
    ...
    assert result["chunks_processed"] == 1


def test_empty_yield_records_empty_marker(session, fake_or_client_empty, fake_embedder):
    result = asyncio.run(extract_chunks(session, fake_or_client_empty, fake_embedder))
    marker = session.exec(select(ChunkExtraction)).first()
    assert marker.status == "empty"


def test_book_filter_scopes_selection(session, fake_or_client, fake_embedder, monkeypatch):
    monkeypatch.setenv("GRIMOIRE_EXTRACT_BOOK", "book-eval")
    # two chunks: book-eval and book-other; only book-eval is processed
    ...
    assert result["chunks_processed"] == 1
```

`fake_or_client` needs a `.model` attribute (selection reads it) and an `extract()` coroutine returning a canned dict. Reuse/extend whatever fake the existing tests use; if the current fake lacks `.model`, add it.

**Step 2: Verify tests fail** (selection still keyed on mentions; no `_prompt_hash`; no markers written).

**Step 3: Implement.** Add the hash helper and thread the book filter + model through selection:

```python
import hashlib

def _prompt_hash() -> str:
    return hashlib.sha256(EXTRACTION_PROMPT.encode("utf-8")).hexdigest()


def _select_pending_chunks(
    session: Session, model: str, prompt_hash: str, limit: int
) -> list[KnowledgeChunk]:
    """Chunks with no chunk_extraction marker for THIS (model, prompt_hash),
    oldest first. Optional GRIMOIRE_EXTRACT_BOOK scopes to one book_id for
    staged eval runs."""
    marker_exists = (
        select(ChunkExtraction.chunk_id)
        .where(
            ChunkExtraction.chunk_id == KnowledgeChunk.id,
            ChunkExtraction.model == model,
            ChunkExtraction.prompt_hash == prompt_hash,
        )
        .exists()
    )
    query = select(KnowledgeChunk).where(~marker_exists)
    book = os.environ.get("GRIMOIRE_EXTRACT_BOOK")
    if book:
        query = query.where(KnowledgeChunk.book_id == book)
    query = query.order_by(KnowledgeChunk.created_at).limit(limit)
    return list(session.execute(query).scalars().all())
```

In `extract_chunks`, compute `prompt_hash = _prompt_hash()` and `model = or_client.model` once, pass them to `_select_pending_chunks`, and after a chunk's `_apply_extraction` succeeds, upsert a marker:

```python
counts = _apply_extraction(session, chunk, {...}, newly_created)
status = "ok" if counts["entities_created"] or counts["entities_reused"] \
             or counts["mentions_created"] else "empty"
session.add(ChunkExtraction(
    chunk_id=chunk.id, model=model, prompt_hash=prompt_hash, status=status,
))
# ... existing summary accumulation ...
```

The marker is added inside the same loop iteration that succeeds; the existing `_commit(session)` after the loop persists markers + extraction together. On the `except (OpenRouterError, ValueError)` / invalid-shape paths, `continue` WITHOUT adding a marker (unchanged) so the chunk stays pending. Do not use `model_dump()` on the marker; construct it field-by-field as above.

**Step 4: Verify tests pass.** (Local run not available - reason through the logic; final verification is CI.)

**Step 5: Commit**

```bash
git add projects/monolith/grimoire/extract.py projects/monolith/grimoire/extract_test.py
git commit -m "feat(grimoire): cache-key extraction on (chunk, model, prompt_hash)"
```

---

### Task 3: JSON self-correction retry (fence-strip + one follow-on turn)

**Files:**

- Modify: `projects/monolith/grimoire/extract.py` (`OpenRouterClient.extract`, `_parse_content`, add `_strip_fences`)
- Test: `projects/monolith/grimoire/extract_test.py`

**Step 1: Write failing tests** using a stub httpx transport (or monkeypatched `AsyncClient.post`) that returns scripted response bodies:

````python
def test_extract_strips_markdown_fence():
    # server returns ```json\n{...}\n``` -> parsed without a second call
    ...

def test_extract_self_corrects_once_on_bad_json():
    # first response: invalid JSON; second (correction) response: valid JSON
    # assert exactly TWO POSTs and a parsed dict returned
    ...

def test_extract_raises_after_failed_correction():
    # both responses invalid -> OpenRouterError/ValueError, exactly two POSTs
    ...
````

**Step 2: Verify they fail** (no fence strip; one bad body currently raises immediately, no correction turn).

**Step 3: Implement.** Add local cleanup, then a single correction turn built from the original messages plus the bad output and the parse error. Keep the transient-HTTP retry loop (`EXTRACT_MAX_RETRIES`) unchanged and separate - this correction is a distinct, single retry on _parse_ failure only.

````python
import re

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

def _strip_fences(text: str) -> str:
    """Best-effort removal of a leading/trailing markdown code fence that
    cheaper models sometimes wrap JSON in. Leaves clean JSON untouched."""
    return _FENCE_RE.sub("", text.strip())
````

Refactor `extract` so the HTTP+parse of a given `messages` list is a helper (`_post_and_parse`) that raises `ValueError` on bad JSON (after trying `_strip_fences`). Then:

```python
async def extract(self, chunk_text: str) -> dict:
    messages = [
        {"role": "system", "content": EXTRACTION_PROMPT},
        {"role": "user", "content": chunk_text},
    ]
    try:
        content, parsed = await self._post_and_parse(messages)
        return parsed
    except ValueError as first_err:
        # one self-correction turn using the error signal
        correction = messages + [
            {"role": "assistant", "content": content},
            {"role": "user", "content":
                f"That did not parse as JSON ({first_err}). "
                "Return only the JSON object, no prose or markdown."},
        ]
        _content, parsed = await self._post_and_parse(correction)
        return parsed
```

`_post_and_parse` returns `(raw_content, parsed_dict)` and applies `_strip_fences` before `json.loads`; a still-invalid body raises `ValueError` (carrying the raw content so the caller can echo it). The transient-HTTP retry stays inside `_post_and_parse`'s POST. Note in a comment that on local Qwen (vLLM guided JSON) this path rarely fires; it earns its keep on hosted models.

**Step 4: Verify tests pass** (reason through; CI confirms).

**Step 5: Commit**

```bash
git add projects/monolith/grimoire/extract.py projects/monolith/grimoire/extract_test.py
git commit -m "feat(grimoire): one JSON self-correction retry on parse failure"
```

---

### Task 4: Env-driven endpoint + key, and jobs guard for keyless local Qwen

**Files:**

- Modify: `projects/monolith/grimoire/extract.py` (`OpenRouterClient.__init__`, header build)
- Modify: `projects/monolith/grimoire/jobs.py` (`grimoire_extract_entities` guard + client construction)
- Test: `projects/monolith/grimoire/extract_test.py`, `projects/monolith/grimoire/jobs_test.py` (extend/create)

**Step 1: Write failing tests.**

- extract: constructing `OpenRouterClient(api_key="")` sends NO `Authorization` header; `GRIMOIRE_EXTRACT_BASE_URL` overrides the POST URL.
- jobs: with `GRIMOIRE_EXTRACT_BASE_URL` set to a local URL and no `OPENROUTER_API_KEY`, `grimoire_extract_entities` RUNS (does not skip); with the default OpenRouter base and no key, it SKIPS (preserves today's behavior).

```python
def test_client_omits_auth_header_when_no_key():
    c = OpenRouterClient(api_key="")
    # capture headers on POST -> assert "Authorization" not in headers

def test_base_url_env_override(monkeypatch):
    monkeypatch.setenv("GRIMOIRE_EXTRACT_BASE_URL", "http://local/v1/chat/completions")
    c = OpenRouterClient(api_key="")
    assert c.base_url == "http://local/v1/chat/completions"
```

If `jobs_test.py` does not exist, create it and add a `grimoire_jobs_test` `py_test` target in `projects/monolith/BUILD` (copy an existing `grimoire_*_test` block; new `*_test.py` needs the hand-added target - Format check will pass without it).

**Step 2: Verify they fail.**

**Step 3: Implement.** In `extract.py`:

```python
class OpenRouterClient:
    def __init__(self, *, api_key: str = "", model: str | None = None,
                 base_url: str | None = None):
        self.api_key = api_key
        self.model = model or os.environ.get("GRIMOIRE_EXTRACT_MODEL", DEFAULT_MODEL)
        self.base_url = base_url or os.environ.get(
            "GRIMOIRE_EXTRACT_BASE_URL", OPENROUTER_URL
        )

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
```

Use `self._headers()` where the POST currently builds `headers`. In `jobs.py`, gate on endpoint configuration, not key presence:

```python
base_url = os.environ.get("GRIMOIRE_EXTRACT_BASE_URL", "")
api_key = os.environ.get("OPENROUTER_API_KEY", "")
is_openrouter = (not base_url) or "openrouter.ai" in base_url
if is_openrouter and not api_key:
    logger.warning(
        "grimoire_extract_entities: OpenRouter endpoint but OPENROUTER_API_KEY "
        "unset, skipping run"
    )
    return None
or_client = OpenRouterClient(api_key=api_key)  # base_url/model read from env
```

(A non-OpenRouter `base_url` such as the in-cluster Qwen URL runs with an empty key.)

**Step 4: Verify tests pass.**

**Step 5: Commit**

```bash
git add projects/monolith/grimoire/extract.py projects/monolith/grimoire/jobs.py \
        projects/monolith/grimoire/extract_test.py projects/monolith/grimoire/jobs_test.py \
        projects/monolith/BUILD
git commit -m "feat(grimoire): env-driven extract endpoint; run keyless on local Qwen"
```

---

### Task 5: Point the workflow at local Qwen + chart bump

**Files:**

- Modify: `projects/monolith/deploy/values.yaml` (extraction CronWorkflow env)
- Modify: `projects/monolith/chart/Chart.yaml` (version bump)
- Modify: `projects/monolith/deploy/application.yaml` (`targetRevision` to match)

**Step 1: Set the workflow env.** Find the `jobs.cronWorkflows` entry for `grimoire-extract-entities` (in `chart/values.yaml` and/or `deploy/values.yaml`) and add env so the extraction pod uses in-cluster Qwen:

```yaml
env:
  - name: GRIMOIRE_EXTRACT_BASE_URL
    value: "http://inference.inference.svc.cluster.local:8080/v1/chat/completions"
  - name: GRIMOIRE_EXTRACT_MODEL
    value: "qwen3.6-27b"
  # GRIMOIRE_EXTRACT_LIMIT / GRIMOIRE_EXTRACT_BOOK left unset (defaults / manual staging)
```

Do NOT change `DEFAULT_MODEL`/`OPENROUTER_URL` in code - deployment wiring via values is the source of truth (keeps OpenRouter as the neutral code fallback). Confirm the extraction CronWorkflow stays `suspend: true` (manual-only).

**Step 2: Render to verify** the env lands on the workflow:

```bash
helm template monolith projects/monolith/chart/ -f projects/monolith/chart/values.yaml \
  -f projects/monolith/deploy/values.yaml | grep -A3 GRIMOIRE_EXTRACT_BASE_URL
```

Expected: the base_url + model env on the grimoire-extract workflow template.

**Step 3: Bump the chart version** in `chart/Chart.yaml` and the matching `targetRevision` in `deploy/application.yaml` (keep them equal).

**Step 4: Commit**

```bash
git add projects/monolith/chart/values.yaml projects/monolith/deploy/values.yaml \
        projects/monolith/chart/Chart.yaml projects/monolith/deploy/application.yaml
git commit -m "feat(grimoire): run entity extraction on in-cluster qwen3.6-27b"
```

---

### Task 6: Push, watch CI, open PR

**Step 1:** `git push -u origin feat/grimoire-extract-qwen-cachekey`

**Step 2:** Open the PR (`gh pr create`) summarizing: local-Qwen extraction (free), `(chunk, model, prompt_hash)` cache key with `ok`/`empty` markers, JSON self-correction retry, `GRIMOIRE_EXTRACT_BOOK` staging filter. Note the deliberate v1 limitation (no content-hash key) and the entity-dedup A/B caveat.

**Step 3:** `gh pr checks <n> --watch`. On red, fetch the BuildBuddy log via `mcp__buildbuddy__get_invocation` (commitSha selector) → `get_target` → `get_log`, quote the actual failure, fix, push. Likely watch-points: the new `py_test` target registration, `atlas.sum` rehash, SQLite CHECK mirroring, and any test asserting the old mention-based dedup or the old numeric config (grep the test tree for stale assertions).

**Step 4:** One end-of-PR comprehensive code review (Opus) against the full diff, then rebase-merge on green (`gh pr merge --rebase`).

---

## Post-merge validation (manual, out of band)

1. Confirm `grimoire.chunk_extraction` exists after the migration applies (ArgoCD sync).
2. Load a tiny FRESH eval book into `s3://grimoire/chunks/<eval>.ndjson`, set `GRIMOIRE_EXTRACT_BOOK=<eval>`, trigger the suspended `grimoire-extract-entities` workflow once, and confirm markers + entities appear and a re-trigger is a no-op (idempotent). Watch pod logs for JSON self-correction warnings (signal of prompt/model JSON friction).
