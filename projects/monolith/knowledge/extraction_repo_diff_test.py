"""Network-free tests for applying authoritative repository comparisons."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlmodel import Session, SQLModel, create_engine, select
import yaml

from knowledge.extraction import (
    REPO_DIFF_PATCH_CAP,
    ExtractionOutputInvalid,
    _repo_diff_cursor_update_sql,
    _repo_diff_rows_sql,
    apply_repo_diff,
    build_repo_diff_prompt,
    ensure_repo_diff_job,
    reconcile_repo_diff_gaps,
    repo_diff_raw_content,
    sweep_unqueued_raws,
)
from knowledge.models import RawInput
from knowledge.repo_diff_source import (
    RepoDiffEvidence,
    RepoDiffRangeInvalid,
    RepoDiffSourceUnavailable,
    collect_repo_diff,
)

BASE = "a" * 40
HEAD = "b" * 40


@pytest.fixture(name="session")
def session_fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'repo-diff.db'}")
    original_schemas = {}
    for table in SQLModel.metadata.tables.values():
        if table.schema is not None:
            original_schemas[table.name] = table.schema
            table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            session.execute(
                text(
                    """
                    CREATE TABLE routine_jobs (
                        name TEXT PRIMARY KEY,
                        routine_kind TEXT NOT NULL,
                        interval_secs INTEGER,
                        next_run_at TIMESTAMP,
                        last_run_at TIMESTAMP,
                        last_status TEXT,
                        payload TEXT,
                        created_by TEXT
                    )
                    """
                )
            )
            session.commit()
            yield session
    finally:
        for table in SQLModel.metadata.tables.values():
            if table.name in original_schemas:
                table.schema = original_schemas[table.name]


def _evidence(base_sha=BASE, head_sha=HEAD):
    patch = "diff --git a/example.py b/example.py\n@@ -1 +1 @@\n-old\n+verified\n"
    return RepoDiffEvidence(
        base_sha=base_sha.lower(),
        head_sha=head_sha.lower(),
        compare_status="ahead",
        total_commits=2,
        diff_stat="example.py | 2 +1 -1",
        patch=patch,
        changed_files=1,
        additions=1,
        deletions=1,
        coverage={
            "files_listed": 1,
            "files_excluded": 0,
            "files_included": 1,
            "files_patch_included": 1,
            "files_patch_omitted_by_github": 0,
            "files_patch_cut_by_cap": 0,
            "patch_chars": len(patch),
            "patch_truncated": False,
            "file_list_complete": True,
            "total_commits": 2,
        },
    )


@pytest.fixture(autouse=True)
def source(monkeypatch):
    """No applier test may accidentally contact GitHub or content storage."""
    calls = SimpleNamespace(compare=[], verify=[], uploads={})

    def collect(base_sha, head_sha):
        calls.compare.append((base_sha, head_sha))
        return _evidence(base_sha, head_sha)

    monkeypatch.setattr("knowledge.extraction.collect_repo_diff", collect)
    monkeypatch.setattr("knowledge.extraction.verify_on_main", calls.verify.append)
    monkeypatch.setattr(
        "knowledge.raw_write.upload_raw",
        lambda raw_id, content: calls.uploads.update({raw_id: content}),
    )
    return calls


def _scout_job(session: Session, last_sha=BASE) -> None:
    session.execute(
        text(
            """
            INSERT INTO routine_jobs
                (name, routine_kind, interval_secs, next_run_at, payload, created_by)
            VALUES ('kg-repo-diff', 'kg-drain', 3600, CURRENT_TIMESTAMP, :payload, 'test')
            """
        ),
        {"payload": json.dumps({"mode": "repo-diff", "last_sha": last_sha})},
    )
    session.commit()


def _output(*, base_sha=BASE, head_sha=HEAD, diff_stat="", diff=""):
    return (
        "```json\n"
        + json.dumps(
            {
                "head_sha": head_sha,
                "base_sha": base_sha,
                "diff_stat": diff_stat,
                "diff": diff,
            }
        )
        + "\n```"
    )


def _payload_bytes(session):
    return session.execute(
        text("SELECT payload FROM routine_jobs WHERE name = 'kg-repo-diff'")
    ).scalar_one()


def _stored_payload(session):
    return json.loads(_payload_bytes(session))


def _set_payload(session, payload):
    session.execute(
        text("UPDATE routine_jobs SET payload = :payload WHERE name = 'kg-repo-diff'"),
        {"payload": json.dumps(payload)},
    )
    session.commit()


def _assert_unchanged(session, before):
    assert _payload_bytes(session) == before
    assert session.exec(select(RawInput)).all() == []
    assert "rejections" not in _stored_payload(session)
    assert session.execute(text("SELECT count(*) FROM routine_jobs")).scalar_one() == 1


def test_scout_prompt_renders_null_cursor_branch():
    prompt = build_repo_diff_prompt(None)
    assert "prior cursor is null" in prompt
    assert "This first run only establishes" in prompt
    assert '"base_sha": "full SHA or null"' in prompt
    assert prompt.rstrip().endswith("```")


def test_scout_prompt_renders_set_cursor_branch():
    prompt = build_repo_diff_prompt(BASE)
    assert BASE in prompt
    assert "git diff --stat <last_sha>..HEAD" in prompt
    assert "pnpm-lock.yaml" in prompt
    assert "requirements*.txt" in prompt
    assert "everything under `bazel-*`" in prompt
    assert "60000 characters" in prompt
    assert "[... elided ...]" in prompt


@pytest.mark.parametrize(
    ("diff", "stat"),
    [
        ("[... elided ...]", "145 files changed, 13202 insertions"),
        ("  \n[... elided ...]\n ", "invented stat"),
        ("headerless model summary", "invented stat"),
        ("diff --git a/invented.py b/invented.py\n+model", "invented stat"),
        ("model patch without statistics", ""),
        ("", "model statistics without patch"),
        ("", ""),
    ],
)
def test_apply_uses_only_authoritative_content(session, source, diff, stat):
    _scout_job(session)
    applied = apply_repo_diff(
        session, "kg-repo-diff", _output(diff=diff, diff_stat=stat)
    )
    raw = session.exec(select(RawInput)).one()
    content = source.uploads[raw.raw_id]
    evidence = _evidence()
    expected_content, extra = repo_diff_raw_content(evidence)
    assert content == expected_content
    assert evidence.diff_stat in content
    assert evidence.patch.rstrip() in content
    if diff.strip():
        assert diff.strip() not in content
    if stat:
        assert stat not in content
    assert applied["created"] is True
    assert applied["raw_id"] == raw.raw_id
    assert applied["changed_files"] == 1
    assert raw.source == "repo-diff"
    assert raw.original_path == f"repo-diff:{BASE}..{HEAD}"
    for key, value in extra.items():
        assert raw.extra[key] == value
    assert raw.extra["evidence_source"] == "github-compare"
    frontmatter = yaml.safe_load(content.split("---\n", 2)[1])
    for key, value in extra.items():
        assert frontmatter[key] == value
    assert frontmatter["compare_status"] == "ahead"
    assert frontmatter["title"] == "main diff aaaaaaa..bbbbbbb"
    jobs = (
        session.execute(text("SELECT name FROM routine_jobs ORDER BY name"))
        .scalars()
        .all()
    )
    assert jobs == ["kg-repo-diff", f"kg:{raw.raw_id}"]
    assert _stored_payload(session) == {"mode": "repo-diff", "last_sha": HEAD}
    assert source.compare == [(BASE, HEAD)]
    assert source.verify == []


@pytest.mark.parametrize("diff", ["[... elided ...]", "model patch", ""])
@pytest.mark.parametrize(
    "error",
    [
        RepoDiffSourceUnavailable("GitHub unavailable"),
        RepoDiffRangeInvalid("diverged"),
        RepoDiffRangeInvalid("behind"),
        RepoDiffRangeInvalid("GitHub rejected comparison (404)"),
        RepoDiffRangeInvalid("Requested head is not reachable from main"),
    ],
)
def test_source_failures_never_write_or_skip(session, monkeypatch, source, diff, error):
    _scout_job(session)
    before = _payload_bytes(session)

    def fail(*_args):
        raise error

    monkeypatch.setattr("knowledge.extraction.collect_repo_diff", fail)
    # Repeated errors must never recover by acknowledging an unverified head.
    for _ in range(4):
        with pytest.raises(type(error)) as caught:
            apply_repo_diff(session, "kg-repo-diff", _output(diff=diff))
        assert caught.value is error
        _assert_unchanged(session, before)
    assert source.uploads == {}


@pytest.mark.parametrize("base_sha", [None, "c" * 40])
@pytest.mark.parametrize("diff", ["", "[... elided ...]"])
def test_mismatched_base_rejected_before_source(session, source, base_sha, diff):
    _scout_job(session)
    before = _payload_bytes(session)
    with pytest.raises(ExtractionOutputInvalid):
        apply_repo_diff(session, "kg-repo-diff", _output(base_sha=base_sha, diff=diff))
    _assert_unchanged(session, before)
    assert source.compare == source.verify == []


@pytest.mark.parametrize("kind", ["identical", "excluded-only"])
def test_valid_empty_comparison_advances_without_raw(
    session, monkeypatch, source, kind
):
    head = BASE if kind == "identical" else HEAD
    _scout_job(session)
    evidence = replace(
        _evidence(head_sha=head),
        compare_status="identical" if kind == "identical" else "ahead",
        total_commits=0 if kind == "identical" else 2,
        changed_files=0,
        additions=0,
        deletions=0,
        diff_stat="",
        patch="",
        coverage={"files_excluded": 0 if kind == "identical" else 1},
    )
    monkeypatch.setattr(
        "knowledge.extraction.collect_repo_diff", lambda *_args: evidence
    )
    applied = apply_repo_diff(
        session, "kg-repo-diff", _output(head_sha=head, diff="invented model patch")
    )
    assert applied == {"raw_id": None, "changed_files": 0, "summary": "no changes"}
    assert _stored_payload(session)["last_sha"] == head
    assert session.exec(select(RawInput)).all() == []
    assert source.uploads == {}


@pytest.mark.parametrize(
    "stored", [{"last_sha": None}, {}, {"last_sha": None, "skipped": []}]
)
def test_first_run_verifies_head_and_initializes_null_safe_cursor(
    session, source, stored
):
    _scout_job(session, None)
    _set_payload(session, {"mode": "repo-diff", **stored})
    applied = apply_repo_diff(
        session,
        "kg-repo-diff",
        _output(base_sha=None, head_sha=HEAD.upper(), diff="model"),
    )
    assert source.verify == [HEAD.upper()]
    assert source.compare == []
    assert applied["summary"] == "no changes"
    assert _stored_payload(session)["last_sha"] == HEAD
    if "skipped" in stored:
        assert _stored_payload(session)["skipped"] == []
    assert session.exec(select(RawInput)).all() == []


@pytest.mark.parametrize(
    "error", [RepoDiffSourceUnavailable("offline"), RepoDiffRangeInvalid("off main")]
)
def test_unverified_first_run_keeps_null_cursor(session, monkeypatch, error):
    _scout_job(session, None)
    before = _payload_bytes(session)

    def fail(_sha):
        raise error

    monkeypatch.setattr("knowledge.extraction.verify_on_main", fail)
    with pytest.raises(type(error)):
        apply_repo_diff(session, "kg-repo-diff", _output(base_sha=None))
    _assert_unchanged(session, before)


def test_large_comparison_persists_bounded_patch_and_coverage(
    session, monkeypatch, source
):
    _scout_job(session)
    body = {
        "status": "ahead",
        "base_commit": {"sha": BASE},
        "merge_base_commit": {"sha": BASE},
        "commits": [{"sha": HEAD}],
        "total_commits": 2,
        "files": [
            {
                "filename": "small.py",
                "additions": 1,
                "patch": "@@ -0,0 +1 @@\n+verified",
            },
            {
                "filename": "large.py",
                "additions": 1,
                "patch": "+" + "x" * REPO_DIFF_PATCH_CAP,
            },
        ],
    }

    def response(request):
        if request.url.path.endswith("...main"):
            return httpx.Response(
                200,
                json={
                    "status": "identical",
                    "base_commit": {"sha": HEAD},
                    "total_commits": 0,
                },
            )
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(response)) as client:
        evidence = collect_repo_diff(BASE, HEAD, client=client)
    monkeypatch.setattr(
        "knowledge.extraction.collect_repo_diff", lambda *_args: evidence
    )
    apply_repo_diff(session, "kg-repo-diff", _output(diff="model"))
    raw = session.exec(select(RawInput)).one()
    content = source.uploads[raw.raw_id]
    persisted_patch = content.split("```diff\n", 1)[1].rsplit("\n```", 1)[0]
    assert len(persisted_patch) <= REPO_DIFF_PATCH_CAP
    assert "[... elided ...]" in persisted_patch
    assert "diff --git a/small.py b/small.py" in persisted_patch
    assert "diff --git a/large.py" not in persisted_patch
    assert raw.extra["coverage"] == evidence.coverage
    assert raw.extra["coverage"]["patch_truncated"] is True
    assert raw.extra["coverage"]["files_patch_cut_by_cap"] == 1
    assert yaml.safe_load(content.split("---\n", 2)[1])["coverage"] == evidence.coverage


def test_duplicate_result_rejected_with_exactly_one_raw(session, source):
    _scout_job(session)
    result = _output(diff="[... elided ...]")
    apply_repo_diff(session, "kg-repo-diff", result)
    before = _payload_bytes(session)
    with pytest.raises(ExtractionOutputInvalid, match="does not match the stored"):
        apply_repo_diff(session, "kg-repo-diff", result)
    assert len(session.exec(select(RawInput)).all()) == 1
    assert _payload_bytes(session) == before
    assert source.compare == [(BASE, HEAD)]


def test_duplicate_authoritative_content_deduplicates(session):
    _scout_job(session)
    assert apply_repo_diff(session, "kg-repo-diff", _output())["created"] is True
    _set_payload(session, {"mode": "repo-diff", "last_sha": BASE})
    assert apply_repo_diff(session, "kg-repo-diff", _output())["created"] is False
    assert len(session.exec(select(RawInput)).all()) == 1
    assert _stored_payload(session)["last_sha"] == HEAD


@pytest.mark.parametrize("initial", [False, True])
def test_concurrent_cursor_move_preserved_without_raw(
    session, monkeypatch, source, initial
):
    _scout_job(session, None if initial else BASE)
    newer = {
        "mode": "repo-diff",
        "last_sha": "c" * 40,
        "skipped": [{"reason": "retain"}],
    }

    def advance(*_args):
        # A committed intervening writer is visible to the CAS, not rolled back
        # with this application. This seam models the read/write race directly.
        _set_payload(session, newer)
        return _evidence()

    monkeypatch.setattr(
        "knowledge.extraction.verify_on_main"
        if initial
        else "knowledge.extraction.collect_repo_diff",
        advance,
    )
    with pytest.raises(ExtractionOutputInvalid, match="cursor changed"):
        apply_repo_diff(
            session, "kg-repo-diff", _output(base_sha=None if initial else BASE)
        )
    assert _stored_payload(session) == newer
    assert session.exec(select(RawInput)).all() == []
    assert source.uploads == {}


def test_persistence_error_rolls_back_cursor_and_raw(session, monkeypatch):
    _scout_job(session)
    before = _payload_bytes(session)

    def fail(*_args):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr("knowledge.raw_write.upload_raw", fail)
    with pytest.raises(RuntimeError, match="storage unavailable"):
        apply_repo_diff(session, "kg-repo-diff", _output())
    _assert_unchanged(session, before)


@pytest.mark.parametrize("mode", ["first-run", "changes", "empty"])
@pytest.mark.parametrize(
    "history", [[], [{"reason": "retain"}], [{"index": i} for i in range(15)], None]
)
def test_existing_skip_history_survives_unchanged(session, monkeypatch, mode, history):
    first_run = mode == "first-run"
    _scout_job(session, None if first_run else BASE)
    if mode == "empty":
        evidence = replace(_evidence(), changed_files=0, diff_stat="", patch="")
        monkeypatch.setattr(
            "knowledge.extraction.collect_repo_diff", lambda *_args: evidence
        )
    _set_payload(
        session,
        {
            "mode": "repo-diff",
            "last_sha": None if first_run else BASE,
            "skipped": history,
            "rejections": 2,
            "attempts": 2,
        },
    )
    apply_repo_diff(
        session, "kg-repo-diff", _output(base_sha=None if first_run else BASE)
    )
    assert _stored_payload(session) == {
        "mode": "repo-diff",
        "last_sha": HEAD,
        "skipped": history,
    }


def test_case_insensitive_range_keeps_exact_stored_sha_for_cas(session, source):
    _scout_job(session, BASE.upper())
    apply_repo_diff(session, "kg-repo-diff", _output())
    assert source.compare == [(BASE.upper(), HEAD)]
    assert _stored_payload(session)["last_sha"] == HEAD


def test_missing_job_cannot_create_orphan_raw(session, source):
    with pytest.raises(ExtractionOutputInvalid, match="cursor changed"):
        apply_repo_diff(session, "kg-repo-diff", _output(base_sha=None))
    assert session.exec(select(RawInput)).all() == []
    assert source.uploads == {}


@pytest.mark.parametrize("field", ["head_sha", "base_sha", "diff", "diff_stat"])
@pytest.mark.parametrize("value", [None, 42, True, [], {}])
def test_malformed_scout_fields_fail_before_source(session, source, field, value):
    _scout_job(session)
    before = _payload_bytes(session)
    payload = {"head_sha": HEAD, "base_sha": BASE, "diff": "", "diff_stat": ""}
    payload[field] = value
    with pytest.raises(ExtractionOutputInvalid):
        apply_repo_diff(
            session, "kg-repo-diff", "```json\n" + json.dumps(payload) + "\n```"
        )
    _assert_unchanged(session, before)
    assert source.compare == source.verify == []


def test_malformed_scout_json_raises(session):
    _scout_job(session)
    with pytest.raises(ExtractionOutputInvalid):
        apply_repo_diff(session, "kg-repo-diff", "```json\n{bad}\n```")


def test_postgres_cursor_sql_qualifies_schema_and_casts_jsonb():
    session = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=postgresql.dialect())
    )
    sql = _repo_diff_cursor_update_sql(session)
    assert sql.startswith(
        "UPDATE claude_agent.routine_jobs SET payload = CAST(:payload AS JSONB)"
    )
    assert "WHERE name = :name AND" in sql
    assert "payload->>'last_sha' = :last_sha" in sql
    assert "payload->>'last_sha' IS NULL AND :last_sha IS NULL" in sql
    assert "json_extract" not in sql
    assert set(text(sql).compile(dialect=postgresql.dialect()).params) == {
        "payload",
        "name",
        "last_sha",
    }


def test_repo_diff_job_registration_follows_flag(session, monkeypatch):
    monkeypatch.setenv("KG_REPO_DIFF_ENABLED", "true")
    assert ensure_repo_diff_job(session) is True
    session.commit()
    job = session.execute(text("SELECT interval_secs, payload FROM routine_jobs")).one()
    assert job.interval_secs == 3600
    assert json.loads(job.payload) == {"mode": "repo-diff", "last_sha": None}
    monkeypatch.setenv("KG_REPO_DIFF_ENABLED", "false")
    assert ensure_repo_diff_job(session) is True
    session.commit()
    assert session.execute(text("SELECT name FROM routine_jobs")).all() == []


def test_unknown_scout_hold_survives_feature_flag_toggle(session, monkeypatch):
    monkeypatch.setenv("KG_REPO_DIFF_ENABLED", "true")
    assert ensure_repo_diff_job(session) is True
    session.execute(
        text("""
        UPDATE routine_jobs SET next_run_at = NULL,
            last_status = 'invocation_outcome_unknown',
            payload = '{"mode": "repo-diff", "last_sha": "retain"}'
    """)
    )
    session.commit()
    for enabled in ("false", "true"):
        monkeypatch.setenv("KG_REPO_DIFF_ENABLED", enabled)
        assert ensure_repo_diff_job(session) is False
        session.commit()
    row = session.execute(text("SELECT * FROM routine_jobs")).one()
    assert row.next_run_at is None
    assert row.last_status == "invocation_outcome_unknown"
    assert json.loads(row.payload)["last_sha"] == "retain"


SECOND_BASE = "c" * 40
SECOND_HEAD = "d" * 40
THIRD_BASE = "e" * 40
THIRD_HEAD = "f" * 40

PLACEHOLDER_BODY = (
    "---\ntitle: main diff aaaaaaa..bbbbbbb\n---\n\n"
    "## Diff stat\n\n```text\n145 files changed, 13202 insertions\n```\n\n"
    "## Diff\n\n```diff\n[... elided ...]\n```\n"
)
MODEL_REAL_BODY = (
    "---\ntitle: model diff\n---\n\n"
    "## Diff\n\n```diff\n"
    "diff --git a/legacy.py b/legacy.py\n--- a/legacy.py\n+++ b/legacy.py\n+model\n```\n"
)


@pytest.fixture(name="bodies")
def bodies_fixture(monkeypatch, source):
    """Serve stored raw bodies without touching object storage."""
    store = {}

    def fetch(content_hash):
        if content_hash in store:
            return store[content_hash]
        return source.uploads.get(content_hash)

    monkeypatch.setattr("knowledge.raw_store.fetch_raw", fetch)
    return store


def _enable_reconcile(monkeypatch):
    monkeypatch.setenv("KG_REPO_DIFF_RECONCILE_ENABLED", "true")


def _insert_raw(session, bodies, content, extra, original_url):
    """Insert one repo-diff raw row with string, dict, or NULL extra."""
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    stored = json.dumps(extra) if isinstance(extra, dict) else extra
    session.execute(
        text(
            """
            INSERT INTO raw_inputs
                (raw_id, path, source, original_path, content_hash, created_at, extra)
            VALUES
                (:raw_id, :path, 'repo-diff', :original_url, :content_hash,
                 CURRENT_TIMESTAMP, :extra)
            """
        ),
        {
            "raw_id": content_hash,
            "path": f"raws/{content_hash}.md",
            "original_url": original_url,
            "content_hash": content_hash,
            "extra": stored,
        },
    )
    session.commit()
    bodies[content_hash] = content
    return content_hash


def _placeholder_extra(base_sha=BASE, head_sha=HEAD):
    return {"base_sha": base_sha, "head_sha": head_sha, "repo": "test"}


def _raw_row(session, raw_id):
    return (
        session.execute(
            text(
                "SELECT raw_id, original_path, content_hash, extra"
                " FROM raw_inputs WHERE raw_id = :raw_id"
            ),
            {"raw_id": raw_id},
        )
        .mappings()
        .one()
    )


def _repo_diff_raws(session):
    return session.execute(
        text("SELECT * FROM raw_inputs WHERE source = 'repo-diff' ORDER BY id ASC")
    ).all()


def test_reconcile_flag_off_does_nothing(session, monkeypatch, source, bodies):
    _scout_job(session)
    raw_id = _insert_raw(
        session,
        bodies,
        PLACEHOLDER_BODY,
        _placeholder_extra(),
        f"repo-diff:{BASE}..{HEAD}",
    )
    before_payload = _payload_bytes(session)
    before_row = dict(_raw_row(session, raw_id))
    for value in ("false", "0", ""):
        monkeypatch.setenv("KG_REPO_DIFF_RECONCILE_ENABLED", value)
        assert reconcile_repo_diff_gaps(session) == 0
    monkeypatch.delenv("KG_REPO_DIFF_RECONCILE_ENABLED", raising=False)
    assert reconcile_repo_diff_gaps(session) == 0
    assert source.compare == []
    assert _payload_bytes(session) == before_payload
    assert dict(_raw_row(session, raw_id)) == before_row
    assert len(_repo_diff_raws(session)) == 1


def test_reconcile_placeholder_raw_once(session, monkeypatch, source, bodies):
    _enable_reconcile(monkeypatch)
    _scout_job(session)
    raw_id = _insert_raw(
        session,
        bodies,
        PLACEHOLDER_BODY,
        _placeholder_extra(),
        f"repo-diff:{BASE}..{HEAD}",
    )
    before_payload = _payload_bytes(session)
    before_row = dict(_raw_row(session, raw_id))
    assert reconcile_repo_diff_gaps(session) == 1
    assert source.compare == [(BASE, HEAD)]
    rows = _repo_diff_raws(session)
    assert len(rows) == 2
    created = session.exec(select(RawInput).where(RawInput.raw_id != raw_id)).one()
    assert created.source == "repo-diff"
    assert created.original_path == f"repo-diff:{BASE}..{HEAD}#github-compare"
    assert created.extra["evidence_source"] == "github-compare"
    assert created.extra["reconciles"] == raw_id
    assert created.extra["base_sha"] == BASE
    assert created.extra["head_sha"] == HEAD
    content = source.uploads[created.raw_id]
    assert "diff --git a/example.py b/example.py" in content
    assert "[... elided ...]" not in content
    assert dict(_raw_row(session, raw_id)) == before_row
    assert bodies[before_row["content_hash"]] == PLACEHOLDER_BODY
    assert _payload_bytes(session) == before_payload
    assert (
        session.execute(text("SELECT count(*) FROM atom_raw_provenance")).scalar_one()
        == 0
    )
    jobs = (
        session.execute(text("SELECT name FROM routine_jobs ORDER BY name"))
        .scalars()
        .all()
    )
    assert f"kg:{created.raw_id}" in jobs
    assert reconcile_repo_diff_gaps(session) == 0
    assert source.compare == [(BASE, HEAD)]
    assert len(_repo_diff_raws(session)) == 2


def test_reconcile_skipped_range_once(session, monkeypatch, source, bodies):
    _enable_reconcile(monkeypatch)
    _scout_job(session)
    _set_payload(
        session,
        {
            "mode": "repo-diff",
            "last_sha": SECOND_HEAD,
            "skipped": [{"base_sha": SECOND_BASE, "head_sha": SECOND_HEAD}],
        },
    )
    before_payload = _payload_bytes(session)
    assert reconcile_repo_diff_gaps(session) == 1
    assert source.compare == [(SECOND_BASE, SECOND_HEAD)]
    created = session.exec(select(RawInput)).one()
    assert (
        created.original_path
        == f"repo-diff:{SECOND_BASE}..{SECOND_HEAD}#github-compare"
    )
    assert created.extra["reconciles"] == "skipped"
    assert created.extra["evidence_source"] == "github-compare"
    assert _payload_bytes(session) == before_payload
    assert json.loads(before_payload)["last_sha"] == SECOND_HEAD
    assert reconcile_repo_diff_gaps(session) == 0
    assert source.compare == [(SECOND_BASE, SECOND_HEAD)]


def test_reconcile_source_failure_writes_nothing_and_retries(
    session, monkeypatch, source, bodies
):
    error = RepoDiffSourceUnavailable("GitHub unavailable")
    _enable_reconcile(monkeypatch)
    _scout_job(session)
    raw_id = _insert_raw(
        session,
        bodies,
        PLACEHOLDER_BODY,
        _placeholder_extra(),
        f"repo-diff:{BASE}..{HEAD}",
    )
    before_payload = _payload_bytes(session)

    def fail(*_args):
        raise error

    monkeypatch.setattr("knowledge.extraction.collect_repo_diff", fail)
    before_row = dict(_raw_row(session, raw_id))
    assert reconcile_repo_diff_gaps(session) == 0
    assert len(_repo_diff_raws(session)) == 1
    assert dict(_raw_row(session, raw_id)) == before_row
    assert _payload_bytes(session) == before_payload
    monkeypatch.setattr(
        "knowledge.extraction.collect_repo_diff",
        lambda base_sha, head_sha: _evidence(base_sha, head_sha),
    )
    assert reconcile_repo_diff_gaps(session) == 1
    assert len(_repo_diff_raws(session)) == 2


def test_reconcile_skips_covered_and_real_headers(session, monkeypatch, source, bodies):
    _enable_reconcile(monkeypatch)
    _scout_job(session)
    evidence = _evidence()
    canonical, canonical_extra = repo_diff_raw_content(evidence)
    _insert_raw(
        session, bodies, canonical, canonical_extra, f"repo-diff:{BASE}..{HEAD}"
    )
    _insert_raw(
        session,
        bodies,
        MODEL_REAL_BODY,
        _placeholder_extra(SECOND_BASE, SECOND_HEAD),
        f"repo-diff:{SECOND_BASE}..{SECOND_HEAD}",
    )
    assert reconcile_repo_diff_gaps(session) == 0
    assert source.compare == []
    assert len(_repo_diff_raws(session)) == 2


@pytest.mark.parametrize(
    "extra",
    [
        json.dumps({"base_sha": BASE, "head_sha": HEAD}),
        "not json at all",
        "42",
        "[1, 2]",
        None,
    ],
)
def test_reconcile_extra_shapes(session, monkeypatch, source, bodies, extra):
    """String, dict, and NULL extras reconcile through extra or URL range."""
    _enable_reconcile(monkeypatch)
    _scout_job(session)
    raw_id = _insert_raw(
        session, bodies, PLACEHOLDER_BODY, extra, f"repo-diff:{BASE}..{HEAD}"
    )
    assert reconcile_repo_diff_gaps(session) == 1
    created = session.exec(select(RawInput).where(RawInput.raw_id != raw_id)).one()
    assert created.extra["reconciles"] == raw_id
    assert created.extra["evidence_source"] == "github-compare"


def test_reconcile_null_extra_without_url_range_is_ignored(
    session, monkeypatch, source, bodies
):
    _enable_reconcile(monkeypatch)
    _scout_job(session)
    _insert_raw(session, bodies, PLACEHOLDER_BODY, None, None)
    assert reconcile_repo_diff_gaps(session) == 0
    assert source.compare == []


@pytest.mark.parametrize(
    "entry",
    [
        {"base_sha": None, "head_sha": HEAD},
        {"head_sha": HEAD},
        {"base_sha": BASE, "head_sha": "short"},
        {"base_sha": "not-hex-at-all-000000000000000000000000", "head_sha": HEAD},
        {"base_sha": True, "head_sha": HEAD},
        {"base_sha": ["c" * 40], "head_sha": HEAD},
        {"base_sha": BASE, "head_sha": None},
        "not-a-dict",
        None,
        42,
    ],
)
def test_reconcile_ignores_invalid_skipped_entries(
    session, monkeypatch, source, bodies, entry
):
    _enable_reconcile(monkeypatch)
    _scout_job(session)
    _set_payload(session, {"mode": "repo-diff", "last_sha": HEAD, "skipped": [entry]})
    assert reconcile_repo_diff_gaps(session) == 0
    assert source.compare == []
    assert len(_repo_diff_raws(session)) == 0


def test_reconcile_ignores_non_list_skipped(session, monkeypatch, source, bodies):
    _enable_reconcile(monkeypatch)
    _scout_job(session)
    _set_payload(
        session, {"mode": "repo-diff", "last_sha": HEAD, "skipped": {"base_sha": BASE}}
    )
    assert reconcile_repo_diff_gaps(session) == 0
    assert source.compare == []


def test_reconcile_without_job_row_uses_only_raws(session, monkeypatch, source, bodies):
    _enable_reconcile(monkeypatch)
    _insert_raw(
        session,
        bodies,
        PLACEHOLDER_BODY,
        _placeholder_extra(),
        f"repo-diff:{BASE}..{HEAD}",
    )
    assert reconcile_repo_diff_gaps(session) == 1
    assert len(_repo_diff_raws(session)) == 2


def test_reconcile_limit_is_honoured(session, monkeypatch, source, bodies):
    _enable_reconcile(monkeypatch)
    _scout_job(session)
    _insert_raw(
        session,
        bodies,
        PLACEHOLDER_BODY,
        _placeholder_extra(),
        f"repo-diff:{BASE}..{HEAD}",
    )
    _insert_raw(
        session,
        bodies,
        PLACEHOLDER_BODY.replace("145 files", "146 files"),
        _placeholder_extra(SECOND_BASE, SECOND_HEAD),
        f"repo-diff:{SECOND_BASE}..{SECOND_HEAD}",
    )
    _set_payload(
        session,
        {
            "mode": "repo-diff",
            "last_sha": THIRD_HEAD,
            "skipped": [{"base_sha": THIRD_BASE, "head_sha": THIRD_HEAD}],
        },
    )
    assert reconcile_repo_diff_gaps(session, limit=2) == 2
    assert source.compare == [(BASE, HEAD), (SECOND_BASE, SECOND_HEAD)]
    assert reconcile_repo_diff_gaps(session) == 1
    assert source.compare == [
        (BASE, HEAD),
        (SECOND_BASE, SECOND_HEAD),
        (THIRD_BASE, THIRD_HEAD),
    ]
    assert len(_repo_diff_raws(session)) == 5


@pytest.mark.parametrize("limit", [0, -1, None, True, "2"])
def test_reconcile_invalid_limit_writes_nothing(
    session, monkeypatch, source, bodies, limit
):
    _enable_reconcile(monkeypatch)
    _scout_job(session)
    _insert_raw(
        session,
        bodies,
        PLACEHOLDER_BODY,
        _placeholder_extra(),
        f"repo-diff:{BASE}..{HEAD}",
    )
    assert reconcile_repo_diff_gaps(session, limit=limit) == 0
    assert source.compare == []
    assert len(_repo_diff_raws(session)) == 1


def test_reconcile_missing_body_retries_next_pass(session, monkeypatch, source, bodies):
    _enable_reconcile(monkeypatch)
    _scout_job(session)
    content_hash = hashlib.sha256(PLACEHOLDER_BODY.encode("utf-8")).hexdigest()
    session.execute(
        text(
            """
            INSERT INTO raw_inputs
                (raw_id, path, source, original_path, content_hash, created_at, extra)
            VALUES
                (:raw_id, :path, 'repo-diff', :original_url, :content_hash,
                 CURRENT_TIMESTAMP, :extra)
            """
        ),
        {
            "raw_id": content_hash,
            "path": f"raws/{content_hash}.md",
            "original_url": f"repo-diff:{BASE}..{HEAD}",
            "content_hash": "0" * 64,
            "extra": json.dumps(_placeholder_extra()),
        },
    )
    session.commit()
    assert reconcile_repo_diff_gaps(session) == 0
    assert source.compare == []
    bodies["0" * 64] = PLACEHOLDER_BODY
    assert reconcile_repo_diff_gaps(session) == 1


def test_reconcile_empty_comparison_writes_nothing(
    session, monkeypatch, source, bodies
):
    _enable_reconcile(monkeypatch)
    _scout_job(session)
    _insert_raw(session, bodies, "", _placeholder_extra(), f"repo-diff:{BASE}..{HEAD}")
    empty = replace(_evidence(), changed_files=0, diff_stat="", patch="")
    monkeypatch.setattr("knowledge.extraction.collect_repo_diff", lambda *_args: empty)
    assert reconcile_repo_diff_gaps(session) == 0
    assert reconcile_repo_diff_gaps(session) == 0
    assert len(_repo_diff_raws(session)) == 1


def test_reconcile_permanent_outcomes_do_not_use_the_budget(
    session, monkeypatch, source, bodies
):
    """Empty and invalid ranges ahead of valid targets must not stall the pass."""
    _enable_reconcile(monkeypatch)
    _scout_job(session)
    fourth_base, fourth_head = "8" * 40, "9" * 40
    for index, (base_sha, head_sha) in enumerate(
        [
            (BASE, HEAD),
            (SECOND_BASE, SECOND_HEAD),
            (THIRD_BASE, THIRD_HEAD),
        ]
    ):
        _insert_raw(
            session,
            bodies,
            PLACEHOLDER_BODY.replace("145 files", f"{index} files"),
            _placeholder_extra(base_sha, head_sha),
            f"repo-diff:{base_sha}..{head_sha}",
        )
    _set_payload(
        session,
        {
            "mode": "repo-diff",
            "last_sha": fourth_head,
            "skipped": [{"base_sha": fourth_base, "head_sha": fourth_head}],
        },
    )
    requested = []

    def collect(base_sha, head_sha):
        requested.append((base_sha, head_sha))
        if (base_sha, head_sha) == (BASE, HEAD):
            return replace(_evidence(base_sha, head_sha), changed_files=0)
        if (base_sha, head_sha) == (SECOND_BASE, SECOND_HEAD):
            raise RepoDiffRangeInvalid("diverged")
        return _evidence(base_sha, head_sha)

    monkeypatch.setattr("knowledge.extraction.collect_repo_diff", collect)
    assert reconcile_repo_diff_gaps(session, limit=2) == 2
    assert requested == [
        (BASE, HEAD),
        (SECOND_BASE, SECOND_HEAD),
        (THIRD_BASE, THIRD_HEAD),
        (fourth_base, fourth_head),
    ]
    reconciled = {
        row.original_path
        for row in session.exec(select(RawInput)).all()
        if row.original_path.endswith("#github-compare")
    }
    assert reconciled == {
        f"repo-diff:{THIRD_BASE}..{THIRD_HEAD}#github-compare",
        f"repo-diff:{fourth_base}..{fourth_head}#github-compare",
    }


def test_reconcile_source_outage_stops_the_pass(session, monkeypatch, source, bodies):
    _enable_reconcile(monkeypatch)
    _scout_job(session)
    for base_sha, head_sha in [(BASE, HEAD), (SECOND_BASE, SECOND_HEAD)]:
        _insert_raw(
            session,
            bodies,
            PLACEHOLDER_BODY.replace("145 files", f"{base_sha[0]} files"),
            _placeholder_extra(base_sha, head_sha),
            f"repo-diff:{base_sha}..{head_sha}",
        )
    requested = []

    def collect(base_sha, head_sha):
        requested.append((base_sha, head_sha))
        raise RepoDiffSourceUnavailable("GitHub unavailable")

    monkeypatch.setattr("knowledge.extraction.collect_repo_diff", collect)
    assert reconcile_repo_diff_gaps(session) == 0
    assert requested == [(BASE, HEAD)]


def test_reconcile_dedupes_raw_and_skipped_overlap(
    session, monkeypatch, source, bodies
):
    _enable_reconcile(monkeypatch)
    _scout_job(session)
    raw_id = _insert_raw(
        session,
        bodies,
        PLACEHOLDER_BODY,
        _placeholder_extra(),
        f"repo-diff:{BASE}..{HEAD}",
    )
    _set_payload(
        session,
        {
            "mode": "repo-diff",
            "last_sha": HEAD,
            "skipped": [{"base_sha": BASE, "head_sha": HEAD}],
        },
    )
    assert reconcile_repo_diff_gaps(session) == 1
    created = session.exec(select(RawInput).where(RawInput.raw_id != raw_id)).one()
    assert created.extra["reconciles"] == raw_id
    assert reconcile_repo_diff_gaps(session) == 0


def test_sweep_survives_reconcile_failure(session, monkeypatch, source, bodies):
    _enable_reconcile(monkeypatch)
    _scout_job(session)
    _insert_raw(
        session,
        bodies,
        PLACEHOLDER_BODY,
        _placeholder_extra(),
        f"repo-diff:{BASE}..{HEAD}",
    )

    def boom(*_args):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr("knowledge.extraction.collect_repo_diff", boom)
    assert isinstance(sweep_unqueued_raws(session), int)
    assert len(_repo_diff_raws(session)) == 1


def test_reconcile_scan_sql_qualifies_postgres_schema():
    pg = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=postgresql.dialect()))
    sql = _repo_diff_rows_sql(pg)
    assert "FROM knowledge.raw_inputs WHERE source = :source" in sql
    assert "ORDER BY created_at ASC, id ASC" in sql
    assert set(text(sql).compile(dialect=postgresql.dialect()).params) == {"source"}


def test_reconcile_scan_sql_uses_bare_table_on_sqlite(session):
    assert "FROM raw_inputs WHERE source = :source" in _repo_diff_rows_sql(session)
