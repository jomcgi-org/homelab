"""Thresholds, reservations and uncertain writes use a file-backed ledger."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine, select

from knowledge import audit_feedback as feedback
from knowledge.models import AuditFinding, AuditProcessIssue, AuditRun, Note

NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv("KG_AUDIT_ENABLED", "true")
    monkeypatch.setenv("KG_AUDIT_ISSUES_ENABLED", "true")
    engine = create_engine(f"sqlite:///{tmp_path / 'feedback.db'}")
    schemas = {table: table.schema for table in SQLModel.metadata.tables.values()}
    for table in schemas:
        table.schema = None
    try:
        SQLModel.metadata.create_all(engine)
        yield engine
    finally:
        engine.dispose()
        for table, schema in schemas.items():
            table.schema = schema


def _seed(engine, *, cause="source_wrong", count=5, roots=3, age=0, **extra):
    with Session(engine) as session:
        runs = [
            AuditRun(
                job_name=f"{cause}-{i}", prompt_version="audit/v1", status="complete"
            )
            for i in range(roots)
        ]
        session.add_all(runs)
        session.flush()
        findings = [
            AuditFinding(
                run_id=runs[i % roots].id,
                note_id=f"{cause}-{i}",
                stream="uniform",
                correctness="confirmed",
                cause=cause,
                rationale="Current repo contradicts this note.",
                source_raw_id="raw-evidence",
                source="repo-diff",
                extraction_version="lens/v1",
                created_at=NOW - timedelta(days=age),
            )
            for i in range(count)
        ]
        for finding in findings:
            for name, value in extra.items():
                setattr(finding, name, value)
        session.add_all(findings)
        session.commit()


def _network(monkeypatch, engine):
    writes = []

    def request(method, path, **kwargs):
        # Opening a competing writer proves HTTP runs after the claim committed.
        with Session(engine) as session:
            session.execute(text("BEGIN IMMEDIATE"))
            if method == "POST":
                assert session.exec(select(AuditProcessIssue)).all()
        if method == "GET":
            return {"items": [], "incomplete_results": False}
        writes.append(kwargs["payload"])
        assert path == f"/repos/{feedback.GITHUB_REPO}/issues"
        return {"number": 100 + len(writes)}

    monkeypatch.setattr(feedback, "github_request", request)
    return writes


@pytest.mark.parametrize("count,roots,expected", [(4, 3, 0), (5, 2, 0), (5, 3, 1)])
def test_threshold_boundaries(engine, monkeypatch, count, roots, expected):
    _seed(engine, count=count, roots=roots)
    writes = _network(monkeypatch, engine)
    assert feedback.file_process_issues(engine=engine, now=NOW) == expected
    assert len(writes) == expected


def test_cause_dedupe_and_bounded_evidence(engine, monkeypatch):
    _seed(engine, count=9)
    with Session(engine) as session:
        session.add_all(
            [
                Note(
                    note_id="source_wrong-8",
                    path="note.md",
                    title="A title",
                    content_hash="hash",
                )
            ]
        )
        session.commit()
    writes = _network(monkeypatch, engine)
    assert feedback.file_process_issues(engine=engine, now=NOW) == 1
    assert feedback.file_process_issues(engine=engine, now=NOW + timedelta(days=8)) == 0
    assert len(writes) == 1
    payload = writes[0]
    assert payload["title"] == "knowledge: source wrong (kg-audit)"
    assert "labels" not in payload
    assert "<!-- kg-audit-cause: source_wrong -->" in payload["body"]
    assert "9 defects across 3 distinct root runs" in payload["body"]
    assert payload["body"].count("> Note id:") == 5
    for value in (
        "A title",
        "raw-evidence",
        "repo-diff",
        "lens/v1",
        "audit/v1",
        "Proposed change",
    ):
        assert value in payload["body"]


def test_issue_samples_exclude_private_holds_and_secrets(engine, monkeypatch):
    _seed(engine)
    token = "ghp_" + "A" * 20
    with Session(engine) as session:
        session.add_all(
            [
                Note(
                    note_id="source_wrong-4",
                    path="held.md",
                    title="Held Private Title",
                    content_hash="hash-held",
                    visibility="private",
                    visibility_verified=True,
                ),
                Note(
                    note_id="source_wrong-3",
                    path="secret.md",
                    title=f"Token {token} inside",
                    content_hash="hash-secret",
                ),
                Note(
                    note_id="source_wrong-2",
                    path="public.md",
                    title="Safe public title",
                    content_hash="hash-public",
                    visibility="public",
                ),
            ]
        )
        session.commit()
    writes = _network(monkeypatch, engine)
    assert feedback.file_process_issues(engine=engine, now=NOW) == 1
    assert len(writes) == 1
    body = writes[0]["body"]
    assert "Safe public title" in body
    assert "source_wrong-2" in body
    assert "Held Private Title" not in body
    assert "source_wrong-4" not in body
    assert token not in body
    assert "source_wrong-3" not in body
    assert body.count("> Note id:") == 3


def test_issue_samples_exclude_secret_rationale(engine, monkeypatch):
    _seed(engine)
    secret = "Bearer " + "b" * 16
    with Session(engine) as session:
        finding = session.exec(
            select(AuditFinding).where(AuditFinding.note_id == "source_wrong-0")
        ).one()
        finding.rationale = f"{secret} restates the evidence"
        session.add(finding)
        session.commit()
    writes = _network(monkeypatch, engine)
    assert feedback.file_process_issues(engine=engine, now=NOW) == 1
    assert len(writes) == 1
    body = writes[0]["body"]
    assert secret not in body
    assert "source_wrong-0" not in body
    assert body.count("> Note id:") == 4


def test_weekly_cap_and_expiry(engine, monkeypatch):
    for cause in ("source_wrong", "other", "missing_supersession"):
        _seed(engine, cause=cause)
    writes = _network(monkeypatch, engine)
    assert feedback.file_process_issues(engine=engine, now=NOW) == 2
    assert feedback.file_process_issues(engine=engine, now=NOW + timedelta(days=6)) == 0
    assert feedback.file_process_issues(engine=engine, now=NOW + timedelta(days=8)) == 1
    assert len(writes) == 3


def test_ambiguous_create_only_reconciles_exact_marker(engine, monkeypatch):
    _seed(engine)
    calls = []
    issues = []

    def request(method, *_args, **_kwargs):
        calls.append(method)
        if method == "GET":
            return {"items": issues, "incomplete_results": False}
        raise TimeoutError("response lost after create")

    monkeypatch.setattr(feedback, "github_request", request)
    assert feedback.file_process_issues(engine=engine, now=NOW) == 0
    assert feedback.file_process_issues(engine=engine, now=NOW) == 0
    issues.extend(
        [
            {"number": 70, "body": "unrelated kg-audit-cause source_wrong"},
            {"number": 71, "body": "<!-- kg-audit-cause: source_wrong -->"},
        ]
    )
    assert feedback.file_process_issues(engine=engine, now=NOW) == 1
    assert calls.count("POST") == 1
    with Session(engine) as session:
        row = session.exec(select(AuditProcessIssue)).one()
        assert row.state == "filed" and row.issue_number == 71


def test_crash_after_reservation_never_recreates(engine, monkeypatch):
    _seed(engine)
    claim = feedback._claim(engine, NOW, feedback.audit_settings())
    assert claim is not None
    writes = _network(monkeypatch, engine)
    assert feedback.file_process_issues(engine=engine, now=NOW) == 0
    assert writes == []


def test_closed_issue_reconciles_lost_create_response(engine, monkeypatch):
    _seed(engine)
    calls = []
    created = []

    def request(method, _path, **kwargs):
        calls.append(method)
        if method == "POST":
            created.append(
                {"number": 76, "state": "closed", "body": kwargs["payload"]["body"]}
            )
            raise TimeoutError("response lost, then issue closed")
        return {"items": [] if "is:open" in kwargs["params"].get("q", "") else created}

    monkeypatch.setattr(feedback, "github_request", request)
    assert feedback.file_process_issues(engine=engine, now=NOW) == 0
    assert feedback.file_process_issues(engine=engine, now=NOW) == 1
    assert calls.count("POST") == 1


def test_existing_marker_adopted_without_create(engine, monkeypatch):
    _seed(engine)
    monkeypatch.setattr(
        feedback,
        "github_request",
        lambda method, *_args, **_kwargs: (
            {"items": [{"number": 99, "body": "<!-- kg-audit-cause: source_wrong -->"}]}
            if method == "GET"
            else pytest.fail("recreated existing issue")
        ),
    )
    assert feedback.file_process_issues(engine=engine, now=NOW) == 1


@pytest.mark.parametrize("flag", ["KG_AUDIT_ENABLED", "KG_AUDIT_ISSUES_ENABLED"])
def test_flags_off_do_nothing(engine, monkeypatch, flag):
    _seed(engine)
    monkeypatch.setenv(flag, "false")
    monkeypatch.setattr(
        feedback,
        "github_request",
        lambda *_args, **_kwargs: pytest.fail("flag-off I/O"),
    )
    assert feedback.file_process_issues(engine=engine, now=NOW) == 0
    with Session(engine) as session:
        assert session.exec(select(AuditProcessIssue)).all() == []


@pytest.mark.parametrize(
    "extra",
    [
        {"correctness": "holds", "clarity": "clear", "placement": "ok"},
        {"correctness": "unknown"},
        {"cause": None},
    ],
)
def test_nondefects_excluded(engine, monkeypatch, extra):
    _seed(engine, **extra)
    writes = _network(monkeypatch, engine)
    assert feedback.file_process_issues(engine=engine, now=NOW) == 0
    assert writes == []


def test_old_and_incomplete_runs_excluded(engine, monkeypatch):
    _seed(engine, age=29)
    _seed(engine, cause="other")
    with Session(engine) as session:
        for run in session.exec(
            select(AuditRun).where(AuditRun.job_name.like("other-%"))
        ).all():
            run.status = "prepared"
        session.commit()
    writes = _network(monkeypatch, engine)
    assert feedback.file_process_issues(engine=engine, now=NOW) == 0
    assert writes == []


def test_expansion_counts_distinct_roots(engine, monkeypatch):
    _seed(engine, roots=1)
    with Session(engine) as session:
        root = session.exec(select(AuditRun)).one()
        children = [
            AuditRun(
                job_name=f"x-{i}",
                root_run_id=root.id,
                stream="expansion",
                prompt_version="audit/v1",
                status="complete",
            )
            for i in range(4)
        ]
        session.add_all(children)
        session.flush()
        findings = session.exec(select(AuditFinding).order_by(AuditFinding.id)).all()
        for finding, child in zip(findings[1:], children):
            finding.run_id = child.id
            finding.stream = "expansion"
        session.commit()
        assert feedback.aggregate_causes(session, NOW - timedelta(days=28)) == [
            ("source_wrong", 5, 1)
        ]
    writes = _network(monkeypatch, engine)
    assert feedback.file_process_issues(engine=engine, now=NOW) == 0
    assert writes == []


def test_pending_writes_reserve_weekly_slots(engine, monkeypatch):
    _seed(engine)
    with Session(engine) as session:
        session.add_all(
            [
                AuditProcessIssue(cause_key=cause, state="unresolved", marker=cause)
                for cause in ("other", "missing_supersession")
            ]
        )
        session.commit()
    writes = _network(monkeypatch, engine)
    assert feedback.file_process_issues(engine=engine, now=NOW) == 0
    assert writes == []


def test_concurrent_claims_share_weekly_cap(engine, monkeypatch):
    monkeypatch.setenv("KG_AUDIT_ISSUES_MAX_PER_WEEK", "1")
    _seed(engine)
    _seed(engine, cause="other")
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(
            pool.map(
                lambda _: feedback._claim(engine, NOW, feedback.audit_settings()),
                range(2),
            )
        )
    assert sum(claim is not None for claim in claims) == 1


def test_search_failure_blocks_create_and_is_nonfatal(engine, monkeypatch):
    _seed(engine)
    calls = []

    def request(method, *_args, **_kwargs):
        calls.append(method)
        return {"incomplete_results": True, "items": []}

    monkeypatch.setattr(feedback, "github_request", request)
    assert feedback.file_process_issues(engine=engine, now=NOW) == 0
    assert calls == ["GET"]


@pytest.mark.parametrize("axis", ["clarity", "placement"])
def test_axis_defects_count_with_correct_fact(engine, monkeypatch, axis):
    _seed(
        engine,
        correctness="holds",
        **{axis: "unclear" if axis == "clarity" else "misplaced"},
    )
    writes = _network(monkeypatch, engine)
    assert feedback.file_process_issues(engine=engine, now=NOW) == 1
    assert len(writes) == 1


def test_late_empty_search_does_not_erase_filed_issue(engine):
    _seed(engine)
    feedback._claim(engine, NOW, feedback.audit_settings())
    feedback._settle(engine, "source_wrong", 75, NOW)
    feedback._settle(engine, "source_wrong", None, NOW)
    with Session(engine) as session:
        row = session.exec(select(AuditProcessIssue)).one()
        assert row.state == "filed" and row.issue_number == 75


def test_http_seam_bounds_and_auth(monkeypatch):
    import httpx

    calls = []
    monkeypatch.setenv("GITHUB_API_TOKEN", "test-token")

    def respond(request):
        calls.append(request)
        return httpx.Response(201, json={"number": 12})

    real_client = httpx.Client

    def client(**kwargs):
        assert kwargs["timeout"].connect == 5.0
        assert kwargs["timeout"].read == 20.0
        return real_client(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr(feedback.httpx, "Client", client)
    assert feedback.github_request(
        "POST", "/repos/owner/repo/issues", payload={"title": "test"}
    ) == {"number": 12}
    assert str(calls[0].url) == f"{feedback.GITHUB_API}/repos/owner/repo/issues"
    assert calls[0].headers["Authorization"] == "Bearer test-token"


def test_http_seam_rejects_oversized_response(monkeypatch):
    import httpx

    real_client = httpx.Client
    monkeypatch.setattr(feedback, "_RESPONSE_LIMIT", 10)
    monkeypatch.setattr(
        feedback.httpx,
        "Client",
        lambda **kwargs: real_client(
            **kwargs,
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, content=b"x" * 11)
            ),
        ),
    )
    with pytest.raises(ValueError, match="exceeds limit"):
        feedback.github_request("GET", "/search/issues")
