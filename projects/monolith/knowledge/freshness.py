"""Elapsed-UTC review policy. All policy decisions use the caller's clock."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

POLICY_VERSION = "v1"
STANDARD = "standard-90d/v1"
VOLATILE = "volatile-24h/v1"
MAX_INTERVAL = timedelta(days=90)
_INSTANCE = re.compile(
    r"\b(?:pr|pull request|pull|issue)s?[ \t]*#?\d+\b|(?<![\w/&])#\d{1,7}\b|"
    r"\b[\w.-]+/[\w.-]+#\d{1,7}\b|"
    r"\bgithub\.com/[\w.-]+/[\w.-]+/(?:pull|issues)/\d+|"
    r"\b(?:workflow[ \t]+)?(?:run|job)(?:[ \t]+id)?[ \t]*#?\d{4,}\b",
    re.IGNORECASE,
)
_SHA = re.compile(r"\b[0-9a-f]{7,40}\b")
# All-letter hex needs explicit check-claim context to distinguish a commit
# from durable prose. Supported standalone check templates still expire in 24h.
_CHECK_TERM = (
    r"(?:passing|passed|green|succeeded|success|failing|failed|failure|red|"
    r"pending|running|queued)"
)
_LETTER_SHA = r"(?-i:[a-f]{7,40})"
_LETTER_SHA_CHECK = re.compile(
    rf"\bchecks[ \t]+(?:(?:are[ \t]+)?{_CHECK_TERM}[ \t]+at[ \t]+{_LETTER_SHA}"
    rf"|at[ \t]+{_LETTER_SHA}[ \t]+are[ \t]+{_CHECK_TERM})\b",
    re.IGNORECASE | re.ASCII,
)
_STATE = re.compile(
    r"\b(open|opened|closed|merged|draft|ready|pending|running|queued|failed|"
    r"failing|passing|passed|green|red|head|outstanding|blocked|cancelled|"
    r"approved|rejected|reopened|stopped|succeeded|success|failure|complete|"
    r"completed|finished|todo|in-progress)\b",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"(?<=[.!?;])[ \t]+|\n+")
_PROVENANCE_SECTION = re.compile(
    r"^#{1,6}[ \t]+(?:evidence|provenance|sources?|references?)[ \t]*:?[ \t]*$"
    r".*?(?=^#{1,6}[ \t]+\S|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
# Outstanding work is volatile even when it names no GitHub instance. Keep
# this vocabulary shared with the verifier: lifecycle evidence cannot clear it.
_OPERATIONAL_SUBJECT = (
    r"(?:pilots?|validations?|verifications?|approvals?|rollouts?|deployments?|"
    r"deploy|CronWorkflow|sign[-\u2010-\u2015 ]?offs?|enablement)"
)
_OUTSTANDING_GATE = re.compile(
    # Obligation and operational subject may appear in either order, with
    # arbitrary modifiers or passive wording between them. Do not depend on
    # an action verb being immediately followed by one recognized noun.
    rf"\A(?=[^.!?;\n]*\b{_OPERATIONAL_SUBJECT}\b)"
    r"(?=[^.!?;\n]*\b(?:needs?[ \t]+to|(?:have|has|ought)[ \t]+to|must|"
    r"shall|should|required[ \t]+to|obliged[ \t]+to|necessary|mandatory|"
    r"compulsory|requirement|owes?|awaiting|remains?|remaining|yet[ \t]+to)\b)"
    r"|\b(still[ \t]+(?:required|needs?|needed)|outstanding|blocked[ \t]+on|"
    r"waiting[ \t]+(?:for|on)|must[ \t]+verify|"
    # A direct needs/requires object can have modifiers, but cannot jump a
    # conjunction or a durable rule/gate object to a later operational noun.
    r"(?:needs?|requires?)[ \t]+"
    r"(?:(?!(?:and|or|but|gates?|rules?|polic(?:y|ies))\b)[^\s.!?;:]+[ \t]+)*"
    rf"{_OPERATIONAL_SUBJECT}|"
    rf"{_OPERATIONAL_SUBJECT}[^.!?;\n]*\b"
    r"(?:required|needed|necessary|mandatory)|"
    r"(?:has|have)[ \t]+not[ \t]+been[ \t]+verified|not[ \t]+yet[ \t]+verified)\b"
    r"|\b(?:todo|follow-up)[ \t]*:",
    re.IGNORECASE,
)


def sentences(text: str) -> list[str]:
    return [part for part in _SENTENCE.split(text) if part.strip()]


def has_instance(sentence: str) -> bool:
    """A concrete subject: PR/issue number, run/job id, or a commit SHA."""
    return (
        _INSTANCE.search(sentence) is not None
        or _LETTER_SHA_CHECK.search(sentence) is not None
        or any(
            any(c.isdigit() for c in token) and any(c in "abcdef" for c in token)
            for token in _SHA.findall(sentence)
        )
    )


def utc(value: object) -> datetime | None:
    """Parse trustworthy evidence dates; malformed input has no freshness."""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def classify(*, title: str, content: str | None, now: datetime) -> str:
    """Classify the persisted claim, never an extractor-supplied policy name.

    A claim is volatile when outstanding operational work is asserted, or a
    concrete instance (a PR or issue number, a run or job id, a commit SHA) is
    asserted in a current state within one sentence. A durable rule that
    merely mentions PRs, checks or gates names no
    instance and stays standard. Evidence and provenance sections cite the PRs
    and jobs a claim was observed through, so only the title and claim body
    decide.
    """
    claim = _PROVENANCE_SECTION.sub("", content or "")
    return (
        VOLATILE
        if any(
            _OUTSTANDING_GATE.search(part)
            or (has_instance(part) and _STATE.search(part))
            for part in sentences(f"{title}\n{claim}")
        )
        else STANDARD
    )


def interval(policy: str) -> timedelta:
    return timedelta(hours=24) if policy == VOLATILE else MAX_INTERVAL


def deadline(
    *,
    observed_at: object,
    policy: str,
    now: datetime,
    last_reviewed_at: object = None,
    supplied: object = None,
) -> datetime | None:
    """Future evidence is unknown. Supplied deadlines can only shorten a lease."""
    observed = utc(observed_at)
    reviewed = utc(last_reviewed_at)
    clock = utc(now)
    basis = reviewed or observed
    if clock is None or basis is None or basis > clock:
        return None
    result = basis + interval(policy)
    requested = utc(supplied)
    return min(result, requested) if requested is not None else result


def state(
    *,
    review_after: object,
    observed_at: object,
    last_reviewed_at: object = None,
    now: datetime,
) -> str:
    basis = utc(last_reviewed_at) or utc(observed_at)
    due = utc(review_after)
    clock = utc(now)
    if basis is None or clock is None or basis > clock or due is None:
        return "unknown"
    return "due" if clock >= due else "current"


def metadata(note, *, now: datetime) -> dict:
    """Shared search, detail and recall projection, independent of confidence."""

    def iso(value):
        parsed = utc(value)
        return parsed.isoformat() if parsed is not None else None

    return {
        "review_after": iso(note.review_after),
        "review_policy": note.review_policy,
        "last_reviewed_at": iso(note.last_reviewed_at),
        "observed_at": iso(note.observed_at),
        "freshness": state(
            review_after=note.review_after,
            observed_at=note.observed_at,
            last_reviewed_at=note.last_reviewed_at,
            now=now,
        ),
        "requires_authoritative_observation": note.review_policy == VOLATILE,
    }


def preserve_deadline(note, *, now: datetime, existing=None, supplied=None) -> None:
    """Ordinary writes and backfill can keep or shorten, never renew."""
    policy = classify(title=note.title, content=note.content, now=now)
    if existing is not None:
        note.observed_at = existing.observed_at
        note.last_reviewed_at = existing.last_reviewed_at
        if existing.review_policy == VOLATILE:
            policy = VOLATILE
    note.review_policy = policy
    candidate = deadline(
        observed_at=note.observed_at,
        last_reviewed_at=note.last_reviewed_at,
        policy=policy,
        supplied=supplied,
        now=now,
    )
    prior = (
        utc(existing.review_after) if existing is not None else utc(note.review_after)
    )
    # An already classified unknown row cannot gain a lease through retelling.
    if existing is not None and existing.review_policy is not None and prior is None:
        candidate = None
    note.review_after = min(prior, candidate) if prior and candidate else candidate


def current_predicate(*, now: datetime, model=None):
    """Fail closed at equality, for future observations and transitional rows."""
    from sqlalchemy import and_, func

    from knowledge.models import Note

    model = Note if model is None else model
    basis = func.coalesce(model.last_reviewed_at, model.observed_at)
    return and_(
        model.review_after.is_not(None),
        model.review_after > now,
        basis.is_not(None),
        basis <= now,
    )


def result_current(result: dict, *, now: datetime) -> bool:
    """Recheck a hydrated or derived result before using it as current context."""
    if result.get("verification_state") == "invalidated":
        return False
    end = utc(result.get("valid_until"))
    if result.get("valid_until") is not None and (end is None or end <= utc(now)):
        return False
    return (
        state(
            review_after=result.get("review_after"),
            observed_at=result.get("observed_at"),
            last_reviewed_at=result.get("last_reviewed_at"),
            now=now,
        )
        == "current"
    )


class ReviewCommit(NamedTuple):
    """Result of a renewal attempt; ``reason`` names why it was refused."""

    renewed: bool
    reason: str

    def __bool__(self) -> bool:
        return self.renewed


# Retry timing after a non-success outcome: unavailable backs off exponentially
# to a cap, failed holds for a day. Unsupported has no retry time and waits for
# a new revision (see review_admission._blocked).
BACKOFF_BASE = timedelta(minutes=5)
BACKOFF_CAP = timedelta(hours=6)
FAILED_RETRY = timedelta(hours=24)


def backoff(attempts: int) -> timedelta:
    return min(BACKOFF_BASE * 2 ** min(max(attempts - 1, 0), 16), BACKOFF_CAP)


def latest_outcome(session, note_id: str):
    from sqlmodel import select

    from knowledge.models import ReviewOutcome

    return session.exec(
        select(ReviewOutcome)
        .where(ReviewOutcome.note_id == note_id)
        .order_by(ReviewOutcome.id.desc())
        .limit(1)
    ).first()


def record_outcome(
    session,
    *,
    note_id: str,
    revision: int,
    status: str,
    reason: str,
    now: datetime,
    evidence: list[str] | None = None,
    evidence_observed_at: datetime | None = None,
):
    """Add a durable outcome row to the caller's transaction (no commit).

    Attempts count consecutive non-success rows at the same revision, and the
    retry time follows the status: success none, unsupported none (waits for a
    revision), failed a day, unavailable an exponential backoff.
    """
    from knowledge.models import ReviewOutcome

    previous = latest_outcome(session, note_id)
    attempts = (
        previous.attempts + 1
        if previous is not None
        and previous.status == status
        and previous.note_revision == revision
        and status != "success"
        else 1
    )
    next_attempt = {
        "unavailable": now + backoff(attempts),
        "failed": now + FAILED_RETRY,
    }.get(status)
    row = ReviewOutcome(
        note_id=note_id,
        status=status,
        reason=reason[:500],
        note_revision=revision,
        attempts=attempts,
        evidence=list(evidence or []),
        evidence_observed_at=utc(evidence_observed_at),
        attempted_at=utc(now),
        next_attempt_at=next_attempt,
    )
    session.add(row)
    return row


def commit_successful_review(
    session,
    *,
    note_id: str,
    expected_revision: int,
    expected_content_hash: str,
    evidence: list[str],
    evidence_observed_at: datetime,
    now: datetime,
) -> ReviewCommit:
    """The only operation permitted to renew a lease, inside the caller's transaction.

    It never commits or rolls back the caller's transaction: the renewal and
    its durable outcome row are added to ``session`` so the caller decides when
    both become visible together. A refusal is also recorded (as ``failed``
    with the reason) so a race leaves a trace, and renews nothing. The row is
    locked, and the caller's captured revision must still match: content_hash
    alone cannot see a duplicate retelling, supersession or dispute that
    arrived after the evidence was read. A volatile policy is sticky.
    An unavailable, failed or unsupported check never reaches this function.
    """
    from sqlmodel import select

    from knowledge.models import Note
    from knowledge.store import open_dispute_note_ids

    observed = utc(evidence_observed_at)
    clock = utc(now)

    def refuse(reason: str) -> ReviewCommit:
        # Recorded at the revision the review read, not the current one: a
        # note that moved on since is not held back by a race it already won.
        record_outcome(
            session,
            note_id=note_id,
            revision=expected_revision,
            status="failed",
            reason=reason,
            now=clock,
            evidence=evidence,
            evidence_observed_at=observed,
        )
        return ReviewCommit(False, reason)

    if not evidence:
        return refuse("no_evidence")
    if observed is None or observed > clock:
        return refuse("invalid_evidence_time")
    note = session.exec(
        select(Note)
        .where(Note.note_id == note_id)
        .with_for_update()
        .limit(1)
        .execution_options(populate_existing=True)
    ).first()
    if note is None or note.deleted_at is not None:
        return refuse("note_missing")
    if note.revision != expected_revision or note.content_hash != expected_content_hash:
        return refuse("revision_changed")
    if note.verification_state in {"disputed", "invalidated"} or note_id in (
        open_dispute_note_ids(session, [note_id])
    ):
        return refuse("disputed_or_invalidated")
    if note.valid_until is not None and utc(note.valid_until) <= clock:
        return refuse("superseded_or_expired")
    reviewed = utc(note.last_reviewed_at)
    if reviewed is not None and observed <= reviewed:
        return refuse("duplicate_or_older_review")
    original = utc(note.observed_at)
    if original is not None and observed < original:
        return refuse("evidence_older_than_observation")
    classified = classify(title=note.title, content=note.content, now=clock)
    note.review_policy = (
        VOLATILE if VOLATILE in {note.review_policy, classified} else STANDARD
    )
    note.last_reviewed_at = observed
    note.review_after = deadline(
        observed_at=note.observed_at,
        last_reviewed_at=observed,
        policy=note.review_policy,
        now=clock,
    )
    session.add(note)
    session.flush()
    record_outcome(
        session,
        note_id=note_id,
        revision=note.revision,
        status="success",
        reason="verified",
        now=clock,
        evidence=evidence,
        evidence_observed_at=observed,
    )
    return ReviewCommit(True, "verified")
