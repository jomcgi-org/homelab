"""Bounded model sessions with independent, replay-safe fact review receipts."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, text
from sqlmodel import Session, select

from knowledge import fact_review as review
from knowledge.extraction import ExtractionOutputInvalid
from knowledge.freshness import record_outcome, utc
from knowledge.models import FactReviewBatch, FactReviewRun
from knowledge.review_batch_sources import BatchSources
from knowledge.review_sources import EvidenceUnavailable, EvidenceUnsupported

MAX_BATCH_SIZE = 32
MAX_CLAIM_BYTES = 64_000
MAX_PROMPT_BYTES = 256_000
MAX_OUTPUT_BYTES = 128_000
SOURCE_UNAVAILABLE = "Source review deferred after source unavailability."


def batch_size() -> int:
    value = int(os.environ.get("KG_FACT_REVIEW_BATCH_SIZE", "32"))
    if not 1 <= value <= MAX_BATCH_SIZE:
        raise ValueError("source review batch size must be between 1 and 32")
    return value


def _batch(session, key, *, lock=False):
    query = select(FactReviewBatch).where(FactReviewBatch.invocation_key == key)
    if lock:
        query = query.with_for_update()
    batch = session.exec(query.execution_options(populate_existing=True)).one()
    if batch.status == "abandoned":
        raise ExtractionOutputInvalid("review batch abandoned by operator recovery")
    return batch


def _members(session, keys):
    rows = session.exec(
        select(FactReviewRun).where(FactReviewRun.invocation_key.in_(keys))
    ).all()
    by_key = {row.invocation_key: row for row in rows}
    return [by_key[key] for key in keys]


def _reserve(session, key, now):
    if session.get_bind().dialect.name != "sqlite":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": review.JOB_NAME},
        )
    batch = session.get(FactReviewBatch, key)
    if batch is not None:
        keys = list(batch.member_keys)
        session.commit()
        return keys
    used = session.exec(
        select(func.count())
        .select_from(FactReviewRun)
        .where(
            FactReviewRun.created_at >= now - timedelta(days=1),
            FactReviewRun.note_id.is_not(None),
        )
    ).one()
    limit = min(batch_size(), max(0, review.settings()[0] - used))
    rows, size = [], 0
    for note in review._candidates(session, now, limit=limit):
        claim = {"title": note.title, "content": note.content, "scope": note.scope}
        claim_size = len(json.dumps(claim).encode())
        if rows and size + claim_size > MAX_CLAIM_BYTES:
            break
        size += claim_size
        rows.append(
            FactReviewRun(
                invocation_key=f"{key}:fact:{len(rows) + 1}",
                note_id=str(note.note_id),
                revision=note.revision,
                content_hash=note.content_hash,
                claim=claim,
                created_at=now,
            )
        )
    keys = [row.invocation_key for row in rows]
    session.add_all(
        [*rows, FactReviewBatch(invocation_key=key, member_keys=keys, created_at=now)]
    )
    session.commit()
    return keys


def _failure(session, key, status, reason, now, *, commit=True, identity=None):
    run = session.exec(
        select(FactReviewRun)
        .where(FactReviewRun.invocation_key == key)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if run.status == "abandoned":
        raise ExtractionOutputInvalid("review member abandoned by operator recovery")
    if run.status != "complete":
        outcome = record_outcome(
            session,
            note_id=run.note_id,
            revision=run.revision,
            status=status,
            reason=review.PREFIX + reason,
            now=now,
        )
        if status == "unsupported" and identity:
            from knowledge.review_source_blocks import record_block

            record_block(session, run, outcome, identity, now)
        run.status = "complete"
        run.completed_at = now
        run.result = {
            "note_id": run.note_id,
            "outcome": status,
            "summary": f"Source review: {status}",
            "reason": reason[:300],
        }
    if commit:
        session.commit()


def _prompt(runs):
    # Sources are stored with content-derived IDs. The model may cite only the
    # source IDs explicitly attached to each claim, even when text is shared.
    sources, claims = {}, []
    for index, run in enumerate(runs, 1):
        if not run.sources:
            continue
        sources.update({source["id"]: source for source in run.sources})
        claims.append(
            {
                "id": f"f{index}",
                "claim": run.claim,
                "claim_hash": review._digest(run.claim),
                "source_ids": [source["id"] for source in run.sources],
                "evidence_observed_at": utc(run.observed_at).isoformat(),
            }
        )
    # Reuse the complete-claim evidence rules from the single-fact reviewer.
    # The first member can be a source-only failure; use a prepared template.
    template = FactReviewRun(
        claim={}, sources=[], observed_at=datetime.now(UTC), note_id="template"
    )
    rules = review._prompt(template).split("Return exactly one JSON fence", 1)[0]
    rules = rules.replace(
        "review ONE complete repository fact",
        "review EACH complete repository fact independently",
    ).replace(
        "every source subject supplied", "every source subject assigned to that claim"
    )
    return (
        rules
        + "Each claim has its own allowed source_ids. Evidence for another claim cannot substitute. "
        "Return exactly one JSON object with a reviews array, one entry per supplied claim. "
        "Copy each id and claim_hash exactly. Keep rationale concise while covering every clause. "
        "Do not omit unsupported claims. No prose outside JSON. Shape: "
        '{"reviews":[{"id":"f1","claim_hash":"copied hash","verdict":"holds|changed|unsupported",'
        '"rationale":"complete coverage or limitation","citations":[{"source_id":"copied id","quote":"exact quotation"}]}]}.\n'
        "BEGIN UNTRUSTED REVIEW DATA\n"
        + json.dumps(
            {"claims": claims, "sources": list(sources.values())}, sort_keys=True
        )
        + "\nEND UNTRUSTED REVIEW DATA"
    )


def _finish(session, key, now, *, transaction_guard=None, commit=True):
    if transaction_guard:
        transaction_guard(session)
    batch = _batch(session, key, lock=True)
    runs = _members(session, batch.member_keys)
    if any(run.status != "complete" for run in runs):
        raise RuntimeError("review batch still has unfinished members")
    counts = dict(Counter(run.result.get("outcome", "unknown") for run in runs))
    batch.status, batch.completed_at = "complete", now
    batch.metrics = {
        **batch.metrics,
        "elapsed_seconds": max(0, (now - utc(batch.created_at)).total_seconds()),
    }
    batch.result = {
        "summary": f"Source review: {len(runs)} facts, {counts}",
        "facts": len(runs),
        "outcomes": counts,
        "retry_required": bool(counts) and set(counts) <= {"unavailable"},
    }
    result = dict(batch.result)
    if commit:
        session.commit()
    return result


def prepare(session: Session, key: str, *, fetch=None, now=None) -> str:
    if not review.enabled():
        return review.NO_REVIEW
    fixed_now = now

    def completed_now():
        return utc(fixed_now or datetime.now(UTC))

    now = completed_now()
    from knowledge import review_coverage as coverage
    from knowledge.review_source_blocks import probe

    if coverage.enabled() and session.get(FactReviewBatch, key) is None:
        probe(session, now, fetch=fetch)
    keys = _reserve(session, key, now)
    if not keys:
        _finish(session, key, completed_now())
        return review.NO_REVIEW
    batch = _batch(session, key)
    if batch.status == "complete":
        return (
            SOURCE_UNAVAILABLE
            if batch.result.get("retry_required")
            else review.NO_MODEL
        )
    revision, observed = batch.repo_revision, utc(batch.observed_at)
    session.commit()
    with BatchSources(fetch=fetch) as collector:
        try:
            pinned = revision or collector.pin()
            batch = _batch(session, key, lock=True)
            if batch.repo_revision is None:
                batch.repo_revision, batch.observed_at = pinned, now
            revision, observed = batch.repo_revision, utc(batch.observed_at)
            session.commit()
        except EvidenceUnavailable as exc:
            for member in keys:
                _failure(session, member, "unavailable", str(exc), now)
            batch = _batch(session, key, lock=True)
            batch.metrics = {
                k: batch.metrics.get(k, 0) + v for k, v in collector.metrics().items()
            }
            session.commit()
            _finish(session, key, completed_now())
            return SOURCE_UNAVAILABLE
        for member in keys:
            run = session.get(FactReviewRun, member)
            if run.status == "complete" or run.sources:
                session.commit()
                continue
            claim = dict(run.claim)
            session.commit()
            try:
                if observed is None or now - observed > review.MAX_EVIDENCE_AGE:
                    raise EvidenceUnavailable("batch evidence is stale")
                sources = collector.collect(claim, revision)
                if not sources:
                    raise EvidenceUnsupported("no supported primary source pointers")
                sources = [
                    {
                        **source,
                        "id": "s"
                        + hashlib.sha256(
                            json.dumps(
                                {k: v for k, v in source.items() if k != "id"},
                                sort_keys=True,
                            ).encode()
                        ).hexdigest()[:16],
                    }
                    for source in sources
                ]
            except EvidenceUnavailable as exc:
                _failure(
                    session,
                    member,
                    "unsupported"
                    if isinstance(exc, EvidenceUnsupported)
                    else "unavailable",
                    str(exc),
                    now,
                    identity=getattr(exc, "identity", None),
                )
                continue
            run = session.exec(
                select(FactReviewRun)
                .where(FactReviewRun.invocation_key == member)
                .with_for_update()
                .execution_options(populate_existing=True)
            ).one()
            if run.status == "abandoned":
                raise ExtractionOutputInvalid(
                    "review member abandoned by operator recovery"
                )
            if not run.sources and run.status != "complete":
                run.sources, run.observed_at = sources, observed
                session.flush()
                if len(_prompt(_members(session, keys)).encode()) > MAX_PROMPT_BYTES:
                    run.sources, run.observed_at = [], None
                    session.flush()
                    _failure(
                        session,
                        member,
                        "unavailable",
                        "batch prompt byte budget exhausted",
                        now,
                    )
                else:
                    session.commit()
            else:
                session.commit()
        batch = _batch(session, key, lock=True)
        batch.metrics = {
            k: batch.metrics.get(k, 0) + v for k, v in collector.metrics().items()
        }
        runs = _members(session, keys)
        prompt = (
            _prompt(runs)
            if any(run.sources and run.status != "complete" for run in runs)
            else None
        )
        batch.metrics = {
            **batch.metrics,
            "prompt_bytes": len(prompt.encode()) if prompt else 0,
            "model_facts": sum(bool(run.sources) for run in runs),
        }
        session.commit()
    if prompt is not None:
        return prompt
    result = _finish(session, key, completed_now())
    return SOURCE_UNAVAILABLE if result["retry_required"] else review.NO_MODEL


def _response(text_value):
    if len(text_value.encode()) > MAX_OUTPUT_BYTES:
        return {"error": "batch output exceeds byte bound"}
    value = text_value.strip()
    fence = re.fullmatch(r"```(?:json)?\s*\n(.*?)\n```", value, re.DOTALL)
    if fence:
        value = fence[1]
    try:
        parsed = json.loads(value)
        if (
            not isinstance(parsed, dict)
            or set(parsed) != {"reviews"}
            or not isinstance(parsed["reviews"], list)
        ):
            raise ValueError("one reviews array required")
        if len(parsed["reviews"]) > MAX_BATCH_SIZE:
            raise ValueError("too many review verdicts")
        return parsed
    except (ValueError, TypeError) as exc:
        return {"error": str(exc)[:300]}


def apply(
    session: Session,
    key: str,
    result_text: str,
    *,
    now=None,
    transaction_guard=None,
    cost_usd=None,
) -> dict:
    now = utc(now or datetime.now(UTC))
    if not review.enabled():
        return {"summary": "Source review disabled"}
    if transaction_guard:
        transaction_guard(session)
    batch = _batch(session, key, lock=True)
    if batch.status == "complete":
        if batch.result.get("retry_required"):
            raise ExtractionOutputInvalid("batch had no usable verdicts")
        return {**batch.result, "replayed": True}
    if batch.response is None:
        batch.response = _response(result_text)
        if type(cost_usd) in {int, float} and math.isfinite(cost_usd) and cost_usd >= 0:
            batch.metrics = {**batch.metrics, "model_cost_usd": cost_usd}
    response, keys = dict(batch.response), list(batch.member_keys)
    session.commit()
    items = response.get("reviews", [])
    ids = Counter(
        item.get("id")
        for item in items
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    )
    entries = {
        item["id"]: item
        for item in items
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    for index, member in enumerate(keys, 1):
        if transaction_guard:
            transaction_guard(session)
        run = session.get(FactReviewRun, member)
        if run.status == "complete":
            session.commit()
            continue
        identity = f"f{index}"
        item = entries.get(identity) if ids[identity] == 1 else None
        if item is None:
            _failure(
                session,
                member,
                "unavailable",
                "missing, duplicate or malformed batch verdict",
                now,
            )
            continue
        verdict = {k: v for k, v in item.items() if k != "id"}
        try:
            review.apply(
                session, member, "```json\n" + json.dumps(verdict) + "\n```", now=now
            )
        except ExtractionOutputInvalid:
            # The single-fact owner durably records the rejected verdict. Other
            # facts retain their independent opportunity to renew.
            session.rollback()
    result = _finish(session, key, now, transaction_guard=transaction_guard)
    if result["retry_required"]:
        raise ExtractionOutputInvalid("batch had no usable verdicts")
    return result


def fail(
    session: Session,
    key: str,
    reason: str,
    *,
    transaction_guard=None,
    now=None,
    commit=True,
):
    """Release claims only after a known failure; unknown invocations never call this."""
    now = utc(now or datetime.now(UTC))
    batch = session.get(FactReviewBatch, key)
    if batch is None or batch.status in {"complete", "abandoned"}:
        return
    keys = list(batch.member_keys)
    if commit:
        session.commit()
    for member in keys:
        if transaction_guard:
            transaction_guard(session)
        _failure(session, member, "unavailable", reason[:300], now, commit=commit)
    _finish(session, key, now, transaction_guard=transaction_guard, commit=commit)


def status(session: Session, *, now: datetime) -> dict:
    """Completion rate and resource use without claim text or source bodies."""
    hour = (
        FactReviewRun.completed_at >= now - timedelta(hours=1),
        FactReviewRun.completed_at <= now,
        FactReviewRun.note_id.is_not(None),
    )
    completed = session.exec(
        select(func.count(func.distinct(FactReviewRun.note_id))).where(*hour)
    ).one()
    outcome = FactReviewRun.result["outcome"].as_string()
    outcomes = session.exec(
        select(outcome, func.count(), func.count(func.distinct(FactReviewRun.note_id)))
        .where(*hour)
        .group_by(outcome)
    ).all()
    metrics = FactReviewBatch.metrics
    aggregates = session.exec(
        select(
            func.count(),
            func.sum(metrics["source_requests"].as_integer()),
            func.sum(metrics["source_cache_hits"].as_integer()),
            func.sum(metrics["source_response_bytes"].as_integer()),
            func.sum(metrics["model_cost_usd"].as_float()),
            func.count(metrics["model_cost_usd"].as_float()),
            func.avg(metrics["elapsed_seconds"].as_float()),
        ).where(
            FactReviewBatch.created_at >= now - timedelta(days=1),
            FactReviewBatch.created_at <= now,
        )
    ).one()
    recent = session.exec(
        select(FactReviewBatch).order_by(FactReviewBatch.created_at.desc()).limit(5)
    ).all()
    return {
        "batch_size": batch_size(),
        "last_hour": {
            "distinct_completed": completed,
            "completed_by_outcome": {
                name: {"attempts": count, "distinct_facts": unique}
                for name, count, unique in outcomes
            },
        },
        "last_24h": dict(
            zip(
                (
                    "batches",
                    "source_requests",
                    "source_cache_hits",
                    "source_response_bytes",
                    "model_cost_usd",
                    "batches_with_reported_cost",
                    "mean_batch_seconds",
                ),
                aggregates,
            )
        ),
        "recent": [
            {
                "invocation_key": row.invocation_key,
                "status": row.status,
                "created_at": utc(row.created_at).isoformat(),
                "completed_at": utc(row.completed_at).isoformat()
                if row.completed_at
                else None,
                "facts": len(row.member_keys),
                "result": row.result,
                "metrics": row.metrics,
            }
            for row in recent
        ],
    }


def abandon_for_recomputation(session: Session, key: str) -> dict:
    """Release unfinished claims without inventing verdicts or removing receipts.

    Used only by explicit audited operator recovery under the routine job lock.
    The caller owns commit. Completed members and source evidence are unchanged.
    """
    batch = _batch(session, key, lock=True)
    keys = list(batch.member_keys)
    if (
        not keys
        or len(keys) > MAX_BATCH_SIZE
        or keys != [f"{key}:fact:{i + 1}" for i in range(len(keys))]
    ):
        raise ValueError("Exact bounded review membership required")
    runs = session.exec(
        select(FactReviewRun)
        .where(FactReviewRun.invocation_key.in_(keys))
        .order_by(FactReviewRun.invocation_key)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    if (
        len(runs) != len(keys)
        or batch.status != "prepared"
        or any(run.status not in {"prepared", "complete"} for run in runs)
    ):
        raise ValueError("Review batch is not an unfinished prepared batch")
    unfinished = [run for run in runs if run.status == "prepared"]
    if not unfinished:
        raise ValueError("No unfinished review members to recompute")
    for run in unfinished:
        run.status = "abandoned"
    batch.status = "abandoned"
    session.flush()
    return {
        "abandoned_members": len(unfinished),
        "retained_completed_members": len(runs) - len(unfinished),
    }
