"""Renewal contract, durable outcomes and bounded admission, on a fake clock."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, SQLModel, create_engine, select

from knowledge.freshness import (
    MAX_INTERVAL,
    STANDARD,
    VOLATILE,
    backoff,
    commit_successful_review,
    record_outcome,
    utc,
)
from knowledge.models import Dispute, Note, ReviewOutcome, bump_revision
from knowledge.review_admission import Limits, due_candidates, run_admission
from knowledge.review_verifier import GitHubResponse, GitHubVerifier, SourceUnavailable

REPO = "jomcgi-org/homelab"
NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'review.db'}").execution_options(
        schema_translate_map={
            table.schema: None for table in SQLModel.metadata.tables.values()
        }
    )
    SQLModel.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session(engine):
    with Session(engine) as db:
        yield db


class Clock:
    def __init__(self, now=NOW, step=timedelta(seconds=1)):
        self.now, self.step = now, step

    def __call__(self):
        self.now += self.step
        return self.now


def volatile(name="n1", number=1, **fields):
    defaults = {
        "note_id": name,
        "path": f"{name}.md",
        "title": f"Issue #{number} is open",
        "content_hash": f"hash-{name}",
        "content": "Tracked work.",
        "observed_at": NOW - timedelta(days=2),
        "review_policy": VOLATILE,
        "review_after": NOW - timedelta(days=1),
        "verification_state": "verified",
        "scope": "repo:test/repo",
    }
    defaults.update(fields)
    return Note(**defaults)


def add(session, *notes):
    session.add_all(notes)
    session.commit()
    for row in notes:
        session.refresh(row)
    return notes[0] if len(notes) == 1 else notes


def review(session, row, **overrides):
    arguments = {
        "note_id": row.note_id,
        "expected_revision": row.revision,
        "expected_content_hash": row.content_hash,
        "evidence": ["issue is open"],
        "evidence_observed_at": NOW,
        "now": NOW,
    }
    return commit_successful_review(session, **{**arguments, **overrides})


def outcomes(session, note_id=None):
    query = select(ReviewOutcome).order_by(ReviewOutcome.id)
    if note_id:
        query = query.where(ReviewOutcome.note_id == note_id)
    return session.exec(query).all()


# ---- commit_successful_review ------------------------------------------------


def test_success_renews_from_evidence_time_and_leaves_the_transaction_open(
    engine, session
):
    row = add(session, volatile())
    result = review(session, row, evidence_observed_at=NOW - timedelta(minutes=5))
    assert result.renewed and result.reason == "verified" and bool(result)
    assert utc(row.observed_at) == NOW - timedelta(days=2)
    assert utc(row.last_reviewed_at) == NOW - timedelta(minutes=5)
    assert utc(row.review_after) == NOW - timedelta(minutes=5) + timedelta(hours=24)
    assert row.revision == 0
    # Nothing was committed by the function: another connection sees no change.
    with Session(engine) as other:
        stored = other.exec(select(Note)).one()
        assert stored.last_reviewed_at is None
        assert other.exec(select(ReviewOutcome)).all() == []
    session.rollback()
    assert session.exec(select(Note)).one().last_reviewed_at is None
    assert outcomes(session) == []


def test_renewal_and_success_outcome_commit_atomically_under_the_caller(
    engine, session
):
    row = add(session, volatile())
    review(session, row)
    session.commit()
    with Session(engine) as other:
        stored = other.exec(select(Note)).one()
        recorded = other.exec(select(ReviewOutcome)).one()
        assert utc(stored.last_reviewed_at) == NOW
        assert (recorded.status, recorded.reason) == ("success", "verified")
        assert recorded.note_revision == stored.revision
        assert recorded.evidence == ["issue is open"]
        assert utc(recorded.evidence_observed_at) == NOW


def refusal_cases():
    yield "no_evidence", {"evidence": []}, {}
    yield (
        "invalid_evidence_time",
        {"evidence_observed_at": NOW + timedelta(seconds=1)},
        {},
    )
    yield "invalid_evidence_time", {"evidence_observed_at": None}, {}
    yield "note_missing", {"note_id": "ghost"}, {}
    yield "revision_changed", {"expected_revision": 99}, {}
    yield "revision_changed", {"expected_content_hash": "other"}, {}
    yield "disputed_or_invalidated", {}, {"verification_state": "disputed"}
    yield "disputed_or_invalidated", {}, {"verification_state": "invalidated"}
    yield "superseded_or_expired", {}, {"valid_until": NOW}
    yield "duplicate_or_older_review", {}, {"last_reviewed_at": NOW}
    yield (
        "duplicate_or_older_review",
        {},
        {"last_reviewed_at": NOW + timedelta(hours=1)},
    )
    yield (
        "evidence_older_than_observation",
        {"evidence_observed_at": NOW - timedelta(days=3)},
        {},
    )


@pytest.mark.parametrize(("reason", "overrides", "fields"), list(refusal_cases()))
def test_refusals_renew_nothing_and_leave_a_trace(session, reason, overrides, fields):
    row = add(session, volatile(**fields))
    before = (row.review_after, row.last_reviewed_at, row.review_policy)
    result = review(session, row, **overrides)
    assert not result.renewed and result.reason == reason
    session.commit()
    session.refresh(row)
    assert (row.review_after, row.last_reviewed_at, row.review_policy) == before
    (recorded,) = outcomes(session)
    assert (recorded.status, recorded.reason) == ("failed", reason)


def test_a_retelling_after_admission_blocks_renewal_though_content_hash_is_equal(
    session,
):
    row = add(session, volatile(confidence=0.9))
    captured = row.revision
    # Duplicate retelling: provenance and confidence change, content_hash does not.
    row.confidence = 1.0
    session.add(row)
    session.commit()
    assert row.revision == captured + 1 and row.content_hash == "hash-n1"
    result = review(session, row, expected_revision=captured)
    assert result.reason == "revision_changed"


def test_a_retelling_that_changes_no_column_still_advances_the_revision(session):
    row = add(session, volatile(confidence=1.0))
    captured = row.revision
    bump_revision(row)
    session.commit()
    assert row.revision == captured + 1
    assert review(session, row, expected_revision=captured).reason == (
        "revision_changed"
    )


def test_housekeeping_updates_do_not_move_the_revision(session):
    row = add(session, volatile())
    row.indexed_at = NOW
    row.layout_x = 1.0
    row.review_after = NOW
    session.commit()
    assert row.revision == 0
    row.title = "Issue #1 is closed"
    session.commit()
    assert row.revision == 1


def test_open_dispute_blocks_even_when_state_still_reads_verified(session):
    row = add(session, volatile())
    session.add(Dispute(note_id=row.note_id, reason="wrong"))
    session.commit()
    assert review(session, row).reason == "disputed_or_invalidated"


def test_duplicate_evidence_is_idempotent(session):
    row = add(session, volatile())
    assert review(session, row).renewed
    session.commit()
    again = review(session, row)
    assert again.reason == "duplicate_or_older_review"
    assert utc(row.last_reviewed_at) == NOW


def test_a_volatile_policy_is_never_downgraded_by_renewal(session):
    row = add(session, volatile(title="Stable detail", content="Plain body"))
    assert review(session, row).renewed
    assert row.review_policy == VOLATILE
    assert utc(row.review_after) == NOW + timedelta(hours=24)
    assert utc(row.review_after) < NOW + MAX_INTERVAL


def test_renewal_can_promote_but_a_standard_note_stays_standard(session):
    promoted, plain = add(
        session,
        volatile("p", review_policy=STANDARD, title="PR #5 is open"),
        volatile("s", review_policy=STANDARD, title="Plain", content="Plain"),
    )
    assert review(session, promoted).renewed and review(session, plain).renewed
    assert promoted.review_policy == VOLATILE
    assert plain.review_policy == STANDARD
    assert utc(plain.review_after) == NOW + MAX_INTERVAL


def test_concurrent_connections_cannot_both_renew_the_same_evidence(engine):
    with Session(engine) as seed:
        row = add(seed, volatile())
        captured = (row.revision, row.content_hash)
    with Session(engine) as first, Session(engine) as second:
        first_row = first.exec(select(Note)).one()
        second_row = second.exec(select(Note)).one()
        args = {
            "note_id": "n1",
            "expected_revision": captured[0],
            "expected_content_hash": captured[1],
            "evidence": ["x"],
            "evidence_observed_at": NOW,
            "now": NOW,
        }
        assert commit_successful_review(first, **args).renewed
        first.commit()
        second.expire_all()
        refused = commit_successful_review(second, **args)
        assert refused.reason == "duplicate_or_older_review"
        second.commit()
        assert first_row.id == second_row.id
    with Session(engine) as check:
        assert [o.status for o in outcomes(check)] == ["success", "failed"]


# ---- outcomes and backoff ----------------------------------------------------


def test_outcome_retry_times_follow_status_and_back_off_to_a_cap(session):
    row = add(session, volatile())
    times = []
    for _ in range(12):
        recorded = record_outcome(
            session,
            note_id=row.note_id,
            revision=row.revision,
            status="unavailable",
            reason="http_503",
            now=NOW,
        )
        session.commit()
        times.append((recorded.attempts, utc(recorded.next_attempt_at) - NOW))
    assert times[0] == (1, timedelta(minutes=5))
    assert times[1] == (2, timedelta(minutes=10))
    assert times[-1] == (12, timedelta(hours=6))
    assert backoff(1) < backoff(2) < backoff(5) <= backoff(50) == timedelta(hours=6)
    unsupported = record_outcome(
        session, note_id="n1", revision=0, status="unsupported", reason="r", now=NOW
    )
    failed = record_outcome(
        session, note_id="n1", revision=0, status="failed", reason="r", now=NOW
    )
    assert unsupported.next_attempt_at is None and unsupported.attempts == 1
    assert utc(failed.next_attempt_at) == NOW + timedelta(hours=24)


# ---- admission ---------------------------------------------------------------


class Github:
    def __init__(self):
        self.issues = {}
        self.calls = []
        self.fail = set()

    def __call__(self, path):
        self.calls.append(path)
        number = int(path.rsplit("/", 1)[1])
        if number in self.fail:
            raise SourceUnavailable("ReadTimeout")
        state = self.issues.get(number)
        if state is None:
            return GitHubResponse(404, {})
        return GitHubResponse(200, {"number": number, "state": state})


def admit(session, github, clock, *, apply=True, **limits):
    options = Limits(**limits)
    verifier = GitHubVerifier(
        github, repo=REPO, clock=clock, max_requests=options.max_requests
    )
    return run_admission(
        session, verifier=verifier, clock=clock, limits=options, apply=apply
    )


def seed_mixed(session):
    return add(
        session,
        volatile("confirmed", 1),
        volatile("changed", 2),
        volatile("gone", 3),
        volatile("free", 4, title="Operational acceptance for #4 remains outstanding"),
        volatile("down", 5),
    )


def test_admission_renews_only_what_github_confirms_and_records_every_outcome(session):
    confirmed, changed, gone, free_text, unavailable = seed_mixed(session)
    github = Github()
    github.issues = {1: "open", 2: "closed", 5: "open"}
    github.fail = {5}
    result = admit(session, github, Clock())
    assert result["candidates"] == 5
    assert (
        result["renewed"],
        result["failed"],
        result["unsupported"],
        result["unavailable"],
    ) == (1, 2, 1, 1)
    status = {o.note_id: (o.status, o.reason) for o in outcomes(session)}
    assert status["confirmed"] == ("success", "verified")
    assert status["changed"][0] == "failed"
    assert "is closed, not open" in status["changed"][1]
    assert status["gone"][0] == "failed" and status["gone"][1].startswith("not found")
    assert status["free"] == (
        "unsupported",
        "acceptance gate is not verifiable from GitHub",
    )
    assert status["down"] == ("unavailable", "ReadTimeout")
    session.expire_all()
    assert confirmed.last_reviewed_at is not None
    for untouched in (changed, gone, free_text, unavailable):
        assert untouched.last_reviewed_at is None
        assert utc(untouched.review_after) == NOW - timedelta(days=1)


def test_admission_does_not_renew_a_claim_with_an_uncovered_reference(session):
    row = add(session, volatile(title="PR #1 and PR #2 are open"))
    original_deadline = utc(row.review_after)
    github = Github()
    github.issues = {2: "open"}
    result = admit(session, github, Clock())
    assert result["unsupported"] == 1 and result["renewed"] == 0
    outcome = outcomes(session, row.note_id)[0]
    assert (outcome.status, outcome.reason) == (
        "unsupported",
        "reference #1 is not covered by a verifiable predicate",
    )
    assert github.calls == []
    session.expire_all()
    assert row.last_reviewed_at is None
    assert utc(row.review_after) == original_deadline


def test_admission_does_not_renew_a_partially_named_subset(session):
    """End to end for the subset renewal: #6822 is closed on GitHub, yet the
    note "PRs 6821 and 6822 are open" must not renew on #6821 alone."""
    row = add(session, volatile("subset", 6821, title="PRs 6821 and 6822 are open"))
    original_deadline = utc(row.review_after)
    github = Github()
    github.issues = {6821: "open", 6822: "closed"}
    result = admit(session, github, Clock())
    assert result["unsupported"] == 1 and result["renewed"] == 0
    outcome = outcomes(session, row.note_id)[0]
    assert outcome.status == "unsupported"
    assert "is not covered by a verifiable predicate" in outcome.reason
    assert github.calls == []
    session.expire_all()
    assert row.last_reviewed_at is None
    assert utc(row.review_after) == original_deadline


@pytest.mark.parametrize(
    "gate",
    [
        "Live pilot is still required before enabling.",
        "Still needs a live pilot.",
        "Operational validation is outstanding.",
        "Must verify after deploy.",
        "Blocked on Joe's approval.",
        "Waiting for the rollout.",
        "TODO: run the pilot.",
        "Follow-up: enable the CronWorkflow.",
        "Deployment has not been verified yet.",
        "A pilot is still needed.",
        "We are waiting on live verification.",
        "The rollout requires a pilot.",
        "We need to run a pilot.",
        "The live pilot is necessary before enabling.",
        "We need to perform operational validation.",
        "We must complete the live verification.",
        "We are required to conduct a pilot.",
        "The deployment verification is mandatory.",
        "We need to verify after deploy.",
        "We need to run the bounded post-deploy live pilot.",
        "The pilot needs to be run.",
        "The bounded pilot still needs to be run.",
        "Before enabling, we must finish a bounded pilot.",
        "We are required to complete the scoped production verification.",
        "Joe's final sign-off is necessary before enabling.",
        "The pilot has yet to be conducted.",
        "The pilot has to be run before enabling.",
        "We are obliged to conduct a scoped live pilot.",
        "The production verification is compulsory.",
        "We still owe Joe a scoped pilot.",
        "A bounded pilot remains to be completed.",
        "The rollout requires a bounded pilot.",
        "We need Joe's approval.",
        "The rollout requires sign-off.",
        "We need sign-off from Joe.",
        "We need Joe’s approval.",
        "The rollout requires a written, signed approval.",
        "We need Joe’s final signoff.",
        "We need Joe’s final sign‑off.",
        "The service requires deployment.",
        "We need staged enablement.",
    ],
)
def test_admission_does_not_renew_an_operational_gate(session, gate):
    row = add(session, volatile(title="PR #1 is merged.", content=gate))
    original = utc(row.review_after)
    github = Github()
    result = admit(session, github, Clock())
    assert result["unsupported"] == 1 and result["renewed"] == 0
    assert "acceptance gate" in outcomes(session, row.note_id)[0].reason
    assert github.calls == []
    session.refresh(row)
    assert row.last_reviewed_at is None
    assert utc(row.review_after) == original


@pytest.mark.parametrize(
    "case",
    [
        "pending-with-failure",
        "unassociated",
        "missing",
        "issue",
        "malformed",
        "unavailable",
        "missing-status",
        "missing-conclusion",
        "missing-statuses",
    ],
)
def test_admission_does_not_renew_unestablished_checks(session, case):
    sha = "de02262a35e221804ead81d6e7fe15fa87b416e8"
    title = f"PR #1 checks passed at {sha}"
    if case == "pending-with-failure":
        title = f"Checks are pending at {sha}"
    elif case.startswith("missing-") and case != "missing":
        term = {
            "missing-status": "pending",
            "missing-conclusion": "failing",
            "missing-statuses": "passed",
        }[case]
        title = f"Checks {term} at {sha}"
    row = add(session, volatile(title=title))
    original = utc(row.review_after)
    paths = {
        f"/repos/{REPO}/issues/1": GitHubResponse(
            200, {"number": 1, "state": "open", "pull_request": {}}
        ),
        f"/repos/{REPO}/pulls/1": GitHubResponse(
            200,
            {
                "number": 1,
                "state": "open",
                "merged": False,
                "draft": False,
                "head": {"sha": sha},
            },
        ),
        f"/repos/{REPO}/commits/{sha}/pulls?per_page=100": GitHubResponse(200, []),
        f"/repos/{REPO}/commits/{sha}/check-runs?per_page=100": GitHubResponse(
            200,
            {
                "total_count": 2,
                "check_runs": [
                    {"head_sha": sha, "status": "completed", "conclusion": "failure"},
                    {"head_sha": sha, "status": "in_progress", "conclusion": None},
                ],
            },
        ),
        f"/repos/{REPO}/commits/{sha}/status?per_page=100": GitHubResponse(
            200, {"sha": sha, "total_count": 0, "statuses": []}
        ),
    }
    if case == "missing":
        paths[f"/repos/{REPO}/issues/1"] = GitHubResponse(404, {})
    elif case == "issue":
        paths[f"/repos/{REPO}/issues/1"] = GitHubResponse(
            200, {"number": 1, "state": "open"}
        )
    elif case in {"malformed", "unavailable"}:
        paths[f"/repos/{REPO}/commits/{sha}/pulls?per_page=100"] = GitHubResponse(
            200 if case == "malformed" else 503, {}
        )
    elif case in {"missing-status", "missing-conclusion", "missing-statuses"}:
        run = {"head_sha": sha, "status": "completed", "conclusion": "success"}
        if case != "missing-statuses":
            del run["status" if case == "missing-status" else "conclusion"]
        paths[f"/repos/{REPO}/commits/{sha}/check-runs?per_page=100"] = GitHubResponse(
            200, {"total_count": 1, "check_runs": [run]}
        )
        if case == "missing-statuses":
            paths[f"/repos/{REPO}/commits/{sha}/status?per_page=100"].body[
                "total_count"
            ] = 1
    result = admit(session, paths.__getitem__, Clock())
    assert result["renewed"] == 0
    expected = (
        "unsupported"
        if case == "issue"
        else (
            "unavailable"
            if case
            in {
                "malformed",
                "unavailable",
                "missing-status",
                "missing-conclusion",
                "missing-statuses",
            }
            else "failed"
        )
    )
    assert outcomes(session, row.note_id)[0].status == expected
    session.refresh(row)
    assert row.last_reviewed_at is None
    assert utc(row.review_after) == original


@pytest.mark.parametrize("explicit", [True, False])
def test_concurrent_retellings_cannot_lose_a_revision(engine, explicit):
    with Session(engine) as seed:
        add(seed, volatile(confidence=0.5))
    with Session(engine) as first, Session(engine) as second:
        a = first.exec(select(Note).where(Note.note_id == "n1")).one()
        b = second.exec(select(Note).where(Note.note_id == "n1")).one()
        if explicit:
            bump_revision(a)
        else:
            a.confidence = 0.6
        first.commit()
        first.refresh(a)
        captured = a.revision
        if explicit:
            bump_revision(b)
        else:
            b.confidence = 0.7
        second.commit()
    with Session(engine) as reviewer:
        row = reviewer.exec(select(Note).where(Note.note_id == "n1")).one()
        assert row.revision == captured + 1
        original = utc(row.review_after)
        result = review(reviewer, row, expected_revision=captured)
        assert result.reason == "revision_changed"
        assert utc(row.review_after) == original


def test_dispute_creation_advances_revision_in_the_callers_transaction(session):
    row = add(session, volatile())
    captured = row.revision
    session.add(Dispute(note_id=row.note_id, reason="new evidence"))
    session.flush()
    session.refresh(row)
    assert row.revision == captured + 1
    assert review(session, row, expected_revision=captured).reason == "revision_changed"
    session.rollback()
    session.refresh(row)
    assert row.revision == captured
    assert not session.exec(select(Dispute)).all()


def test_admission_is_idempotent_and_blocked_outcomes_do_not_flood(session):
    rows = seed_mixed(session)
    github = Github()
    github.issues = {1: "open", 2: "closed", 5: "open"}
    github.fail = {5}
    clock = Clock()
    first = admit(session, github, clock)
    assert first["renewed"] == 1 and first["unavailable"] == 1
    calls = len(github.calls)
    # Inside the backoff and the day-long failed/unsupported holds: no requests.
    again = admit(session, github, clock)
    assert again["candidates"] == 0 and again["blocked"] == 4
    assert len(github.calls) == calls
    assert len(outcomes(session)) == 5
    # Still down after the first backoff: one retry, then a longer wait.
    clock.now += timedelta(minutes=6)
    retry = admit(session, github, clock)
    assert retry["candidates"] == 1 and retry["unavailable"] == 1
    assert len(github.calls) == calls + 1
    last = outcomes(session, "down")[-1]
    assert last.attempts == 2
    assert utc(last.next_attempt_at) - utc(last.attempted_at) == timedelta(minutes=10)
    clock.now += timedelta(minutes=11)
    github.fail = set()
    recovered = admit(session, github, clock)
    assert recovered["renewed"] == 1 and recovered["candidates"] == 1
    assert [row.note_id for row in rows if row.last_reviewed_at is not None] == [
        "confirmed",
        "down",
    ]
    assert admit(session, github, clock)["candidates"] == 0


def test_unsupported_and_failed_wait_for_a_new_revision_or_their_hold(session):
    unsupported, changed = add(
        session,
        volatile("free", 1, title="Issue #1 is blocked"),
        volatile("changed", 2),
    )
    github = Github()
    github.issues = {2: "closed"}
    clock = Clock()
    admit(session, github, clock)
    assert admit(session, github, clock)["candidates"] == 0
    clock.now += timedelta(hours=23)
    assert admit(session, github, clock)["candidates"] == 0
    # A retelling moves the revision, so the changed note gets a fresh verdict.
    changed.confidence = 0.5
    session.add(changed)
    session.commit()
    github.issues[2] = "open"
    retried = admit(session, github, clock)
    assert retried["renewed"] == 1 and retried["candidates"] == 1
    # Unsupported has no retry time: only a revision re-admits it, never time.
    clock.now += timedelta(days=30)
    assert admit(session, github, clock)["unsupported"] == 0
    unsupported.title = "Issue #1 is blocked again"
    session.add(unsupported)
    session.commit()
    assert admit(session, github, clock)["unsupported"] == 1


def test_unsupported_outcome_waits_for_a_new_revision_even_across_a_reindex(session):
    from knowledge.frontmatter import ParsedFrontmatter
    from knowledge.store import KnowledgeStore

    store = KnowledgeStore(session=session, now=NOW)

    def reindex(title, content_hash):
        store.upsert_note(
            note_id="n1",
            path="n1.md",
            content_hash=content_hash,
            title=title,
            metadata=ParsedFrontmatter(
                title=title, observed_at=NOW - timedelta(days=2)
            ),
            chunks=[{"index": 0, "section_header": "", "text": "x"}],
            vectors=[[0.0] * 1024],
            links=[],
            content="Tracked work.",
        )

    reindex("Issue #1 is blocked", "h1")
    row = session.exec(select(Note).where(Note.note_id == "n1")).one()
    row.review_after = NOW - timedelta(days=1)
    session.add(row)
    session.commit()
    github, clock = Github(), Clock()
    assert admit(session, github, clock)["unsupported"] == 1
    assert admit(session, github, clock)["candidates"] == 0
    # The note is edited into a verifiable form: the replaced row's revision
    # moved past the one the outcome was recorded at, so it is admitted again.
    reindex("Issue #1 is open", "h2")
    row = session.exec(select(Note).where(Note.note_id == "n1")).one()
    row.review_after = NOW - timedelta(days=1)
    session.add(row)
    session.commit()
    github.issues = {1: "open"}
    assert admit(session, github, clock)["candidates"] == 1


def test_unsupported_notes_never_starve_verifiable_ones(session):
    blocked = [
        volatile(f"free{n}", n, title=f"Issue #{n} is blocked") for n in range(30)
    ]
    good = volatile("good", 99)
    add(session, *blocked, good)
    github = Github()
    github.issues = {99: "open"}
    clock = Clock()
    first = admit(session, github, clock, batch=5)
    assert first["unsupported"] == 5 and first["candidates"] == 5
    for _ in range(8):
        admit(session, github, clock, batch=5)
    session.expire_all()
    assert good.last_reviewed_at is not None
    assert all(row.last_reviewed_at is None for row in blocked)
    assert all(sum(1 for o in outcomes(session, row.note_id)) == 1 for row in blocked)


def test_batch_request_budget_and_deadline_bound_a_run(session):
    add(session, *[volatile(f"n{n}", n) for n in range(1, 9)])
    github = Github()
    github.issues = {n: "open" for n in range(1, 9)}
    clock = Clock()
    batch = admit(session, github, clock, batch=3)
    assert batch["candidates"] == 3 and batch["renewed"] == 3
    budget = admit(session, github, clock, batch=10, max_requests=2)
    assert budget["renewed"] == 2 and budget["budget_exhausted"] == 1
    assert budget["requests"] == 2
    slow = Clock(step=timedelta(seconds=40))
    deadline = admit(session, github, slow, batch=10, deadline_seconds=100)
    assert deadline["deadline_reached"] == 1 and deadline["renewed"] < 3
    with pytest.raises(ValueError):
        Limits(batch=0)
    with pytest.raises(ValueError):
        Limits(max_requests=501)
    with pytest.raises(ValueError):
        Limits(deadline_seconds=0)


def test_dry_run_counts_without_requests_or_writes(session):
    add(session, volatile("a", 1), volatile("b", 2, title="Issue #2 is blocked"))
    github = Github()
    result = admit(session, github, Clock(), apply=False)
    assert result == {"dry_run": True, "candidates": 2, "blocked": 0}
    assert github.calls == [] and outcomes(session) == []


def test_only_due_live_undisputed_volatile_notes_are_admitted(session):
    add(
        session,
        volatile("due", 1),
        volatile("unknown", 2, review_after=None, observed_at=None),
        volatile("current", 3, review_after=NOW + timedelta(hours=1)),
        volatile("standard", 4, review_policy=STANDARD),
        volatile("deleted", 5, deleted_at=NOW),
        volatile("disputed", 6, verification_state="disputed"),
        volatile("expired", 7, valid_until=NOW - timedelta(days=1)),
        volatile("open-dispute", 8),
    )
    session.add(Dispute(note_id="open-dispute", reason="wrong"))
    session.commit()
    found, blocked = due_candidates(session, now=NOW, limit=50)
    assert sorted(c.note_id for c in found) == ["due", "unknown"]
    assert blocked == 0
    assert found[0].note_id == "unknown"


def test_a_race_after_verification_aborts_the_renewal_and_is_recorded(session):
    row = add(session, volatile())
    github = Github()
    github.issues = {1: "open"}
    clock = Clock()

    class Racing(GitHubVerifier):
        def verify(self, **kwargs):
            verdict = super().verify(**kwargs)
            # A retelling lands between reading the note and committing.
            row.confidence = 1.0
            session.add(row)
            session.commit()
            return verdict

    verifier = Racing(github, repo=REPO, clock=clock)
    result = run_admission(
        session, verifier=verifier, clock=clock, limits=Limits(), apply=True
    )
    assert result["aborted"] == 1 and result.get("renewed", 0) == 0
    session.refresh(row)
    assert row.last_reviewed_at is None
    (recorded,) = outcomes(session)
    assert (recorded.status, recorded.reason) == ("failed", "revision_changed")
    # The race is not held against the note: the next run reviews the new revision.
    assert recorded.note_revision == row.revision - 1
    found, blocked = due_candidates(session, now=NOW + timedelta(days=2), limit=5)
    assert [c.note_id for c in found] == ["n1"] and blocked == 0


def test_a_failing_note_is_isolated_but_database_outages_propagate(
    session, monkeypatch
):
    add(session, volatile("a", 1), volatile("b", 2))
    github = Github()
    github.issues = {1: "open", 2: "open"}
    import knowledge.review_admission as module

    real = module.commit_successful_review
    calls = {"n": 0}

    def explode(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real(*args, **kwargs)

    monkeypatch.setattr(module, "commit_successful_review", explode)
    result = admit(session, github, Clock())
    assert result["errors"] == 1 and result["renewed"] == 1
    assert [o.note_id for o in outcomes(session)] == ["b"]

    def outage(*args, **kwargs):
        raise OperationalError("UPDATE", {}, RuntimeError("down"))

    monkeypatch.setattr(module, "commit_successful_review", outage)
    with pytest.raises(OperationalError):
        admit(session, github, Clock(now=NOW + timedelta(days=5)))
