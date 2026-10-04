"""Elapsed-UTC review policy. All policy decisions use the caller's clock."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

POLICY_VERSION = "v1"
STANDARD = "standard-90d/v1"
VOLATILE = "volatile-24h/v1"
MAX_INTERVAL = timedelta(days=90)
_SUBJECT = re.compile(
    r"\b(pr|pull request|issue|job|workflow|check[s]?)\b", re.IGNORECASE
)
_STATE = re.compile(
    r"\b(open|closed|merged|draft|ready|pending|running|queued|failed|passing|"
    r"passed|green|red|head|sha|status|state|outstanding|blocked|cancelled|"
    r"approved|rejected|reopened|stopped|succeeded|success|failure|complete|"
    r"completed|finished|healthy|unhealthy|todo|in-progress)\b",
    re.IGNORECASE,
)
_PROVENANCE_SECTION = re.compile(
    r"^#{1,6}[ \t]+(?:evidence|provenance|sources?|references?)[ \t]*:?[ \t]*$"
    r".*?(?=^#{1,6}[ \t]+\S|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_GATE = re.compile(
    r"\b(acceptance|validation|operational|live|rollout)\b.{0,100}"
    r"\b(outstanding|remaining|pending|gate|unverified|requires|required)\b|"
    r"\b(outstanding|remaining|pending)\b.{0,100}\b(acceptance|validation|gate)\b",
    re.IGNORECASE | re.DOTALL,
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

    Evidence and provenance sections cite the PRs, jobs and checks a durable
    claim was observed through; only the title and claim body say what the
    claim asserts, so only they decide volatility.
    """
    claim = _PROVENANCE_SECTION.sub("", content or "")
    text = f"{title}\n{claim}"
    return (
        VOLATILE
        if (_SUBJECT.search(text) and _STATE.search(text)) or _GATE.search(text)
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


def current_predicate(*, now: datetime):
    """Fail closed at equality, for future observations and transitional rows."""
    from sqlalchemy import and_, func

    from knowledge.models import Note

    basis = func.coalesce(Note.last_reviewed_at, Note.observed_at)
    return and_(
        Note.review_after.is_not(None),
        Note.review_after > now,
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


def commit_successful_review(
    session,
    *,
    note_id: str,
    expected_content_hash: str,
    evidence: list[str],
    evidence_observed_at: datetime,
    now: datetime,
) -> bool:
    """Reserved extension point: the only operation permitted to renew a lease.

    Slice 2 must supply authoritative, predicate-specific evidence and harden
    revision/dispute races with durable review records before wiring admission.
    This core deliberately has no caller or verifier. It preserves observed_at.
    An unavailable/failed check must never call this function.
    """
    from sqlalchemy.exc import OperationalError
    from sqlmodel import select

    from knowledge.models import Note
    from knowledge.store import open_dispute_note_ids

    observed = utc(evidence_observed_at)
    if not evidence or observed is None or observed > utc(now):
        return False
    try:
        with session.begin_nested():
            note = session.exec(
                select(Note)
                .where(Note.note_id == note_id)
                .with_for_update()
                .limit(1)
                .execution_options(populate_existing=True)
            ).first()
            if (
                note is None
                or note.content_hash != expected_content_hash
                or note.deleted_at is not None
                or note.verification_state in {"disputed", "invalidated"}
                or (note.valid_until is not None and utc(note.valid_until) <= utc(now))
                or note_id in open_dispute_note_ids(session, [note_id])
                or (
                    utc(note.last_reviewed_at) is not None
                    and observed <= utc(note.last_reviewed_at)
                )
                or (
                    utc(note.observed_at) is not None
                    and observed < utc(note.observed_at)
                )
            ):
                return False
            note.review_policy = classify(
                title=note.title, content=note.content, now=now
            )
            note.last_reviewed_at = observed
            note.review_after = deadline(
                observed_at=note.observed_at,
                last_reviewed_at=observed,
                policy=note.review_policy,
                now=now,
            )
            session.flush()
        session.commit()
    except OperationalError:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise
    return True
