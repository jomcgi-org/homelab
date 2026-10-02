"""File bounded process proposals, with durable claims before GitHub writes.

An uncertain create is reconciled by its exact marker on later scheduled runs.
Neither an empty search nor a failed request authorizes repeating that create.
No database transaction is held during HTTP I/O.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from html import escape

import httpx
from core.github import GITHUB_API, GITHUB_REPO
from sqlalchemy import func, or_, text, update
from sqlmodel import Session, select

from knowledge.audit import audit_enabled, audit_settings
from knowledge.models import AuditFinding, AuditProcessIssue, AuditRun, Note
from knowledge.redact import redact_text

_LOGGER = logging.getLogger(__name__)
_LOCK_KEY = 6721003
_RESPONSE_LIMIT = 1024 * 1024
_PROPOSALS = {
    "lens_overgeneralised": "Constrain the source lens to the evidence's scope and add an over-generalisation regression case.",
    "missing_supersession": "Teach the extraction prompt to identify the previous assertion and request its supersession.",
    "stale_after_code_change": "Include changed repository paths in revalidation and test the extraction lens against current code.",
    "duplicate_not_merged": "Add duplicate examples to the extraction prompt and check merge candidate selection.",
    "ranking_surfaced_stale": "Test ranking on the stale samples and account for validity and observation age.",
    "chunking_split_evidence": "Keep qualifying evidence with its assertion when chunking and add split-evidence prompt cases.",
    "source_wrong": "Require source corroboration in the extraction lens and add the samples as counterexamples.",
    "other": "Review the sample evidence and add a targeted extraction prompt or ranking regression case.",
}


def github_request(method: str, path: str, *, params=None, payload=None) -> object:
    """One bounded network seam for both reconciliation and creation."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "monolith-kg-audit",
    }
    token = os.environ.get("GITHUB_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with (
        httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0)) as client,
        client.stream(
            method, f"{GITHUB_API}{path}", headers=headers, params=params, json=payload
        ) as response,
    ):
        response.raise_for_status()
        content = bytearray()
        for chunk in response.iter_bytes():
            if len(content) + len(chunk) > _RESPONSE_LIMIT:
                raise ValueError("GitHub audit response exceeds limit")
            content.extend(chunk)
    return json.loads(bytes(content) or b"{}")


def _find_issue(repo: str, marker: str, cause: str) -> int | None:
    # GitHub search is bounded at 1000 hits. Absence never clears an old claim.
    for page in range(1, 11):
        result = github_request(
            "GET",
            "/search/issues",
            params={
                "q": f'repo:{repo} is:issue in:body "kg-audit-cause" "{cause}"',
                "per_page": 100,
                "page": page,
            },
        )
        if not isinstance(result, dict) or result.get("incomplete_results"):
            raise ValueError("incomplete GitHub audit marker search")
        items = result.get("items")
        if not isinstance(items, list):
            raise TypeError("malformed GitHub audit marker search")
        for issue in items:
            if (
                isinstance(issue, dict)
                and "pull_request" not in issue
                and marker in str(issue.get("body") or "")
                and type(issue.get("number")) is int
                and issue["number"] > 0
            ):
                return issue["number"]
        if len(items) < 100:
            return None
    raise ValueError("GitHub audit marker search exceeds limit")


def _defects_since(since: datetime):
    return (
        AuditRun.status == "complete",
        AuditFinding.created_at >= since,
        AuditFinding.cause.is_not(None),
        or_(
            AuditFinding.correctness.in_(
                ("confirmed", "narrowed", "superseded", "invalidated")
            ),
            AuditFinding.clarity == "unclear",
            AuditFinding.placement == "misplaced",
        ),
    )


def aggregate_causes(session: Session, since: datetime) -> list[tuple[str, int, int]]:
    """Expansion findings share their scheduled root for the run threshold."""
    roots = func.coalesce(AuditRun.root_run_id, AuditRun.id)
    return list(
        session.exec(
            select(
                AuditFinding.cause,
                func.count(AuditFinding.id),
                func.count(func.distinct(roots)),
            )
            .join(AuditRun, AuditFinding.run_id == AuditRun.id)
            .where(*_defects_since(since))
            .group_by(AuditFinding.cause)
            .order_by(AuditFinding.cause)
        ).all()
    )


def _publishable_sample() -> object:
    # Mirror the publication policy (knowledge/publish.py): human holds
    # (visibility='private' with visibility_verified) are never published.
    # Rows without a joined note have no visibility to protect and are kept.
    return or_(
        Note.id.is_(None),
        Note.visibility.is_(None),
        Note.visibility != "private",
        Note.visibility_verified.is_not(True),
    )


def _body(
    session: Session, cause: str, defects: int, runs: int, since: datetime
) -> str:
    candidates = session.exec(
        select(
            AuditFinding,
            AuditRun.prompt_version,
            Note.title,
            Note.visibility,
            Note.visibility_verified,
        )
        .join(AuditRun, AuditFinding.run_id == AuditRun.id)
        .outerjoin(Note, AuditFinding.note_id == Note.note_id)
        .where(*_defects_since(since), AuditFinding.cause == cause)
        .where(_publishable_sample())
        .order_by(AuditFinding.created_at.desc(), AuditFinding.id.desc())
        .limit(25)
    ).all()
    samples = []
    for row in candidates:
        finding, prompt, title, visibility, visibility_verified = row
        # Defense in depth: the SQL predicate above already excludes human
        # holds, but never publish one even if the join predicate drifts.
        if visibility == "private" and visibility_verified is True:
            continue
        # Never publish secret-shaped prose to the public repo: drop any
        # sample whose title or rationale trips the shared redactor, the
        # same gate the publication policy applies before auto-publishing.
        title_hit = redact_text(title or "")[1] > 0
        rationale_hit = redact_text(finding.rationale or "")[1] > 0
        if title_hit or rationale_hit:
            _LOGGER.warning(
                "kg audit issue sample skipped note_id=%s reason=redaction_hit",
                finding.note_id,
            )
            continue
        samples.append((finding, prompt, title))
        if len(samples) >= 5:
            break
    lines = [
        f"<!-- kg-audit-cause: {cause} -->",
        "",
        (
            f"The KG audit found {defects} defects across {runs} distinct root runs "
            f"since {since.date().isoformat()}."
        ),
        "",
        "## Sample findings",
    ]
    for finding, prompt, title in samples:
        # Retrieved note prose is evidence, never GitHub Markdown instructions.
        fields = {
            "Note id": finding.note_id,
            "Title": title,
            "Rationale": finding.rationale,
            "Source raw id": finding.source_raw_id,
            "Source lens": finding.source,
            "Extraction version": finding.extraction_version,
            "Prompt version": prompt,
        }
        lines.append("")
        lines.extend(
            f"> {name}: {escape(str(value or 'unknown')[:1200]).replace(chr(10), ' ').replace(chr(13), ' ')}"
            for name, value in fields.items()
        )
    lines.extend(["", "## Proposed change", "", _PROPOSALS[cause]])
    lines.append(
        "\nThis is a process proposal for a normal PR. The audit changed no lens, prompt or ranking."
    )
    return "\n".join(lines)


def _claim(engine, now: datetime, settings) -> tuple[str, str, dict] | None:
    """Serialize both cause dedupe and the weekly slot reservation."""
    with Session(engine) as session:
        if session.get_bind().dialect.name == "sqlite":
            session.execute(text("BEGIN IMMEDIATE"))
        else:
            session.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK_KEY}
            )
        existing = session.exec(select(AuditProcessIssue)).all()
        occupied = sum(
            row.state != "filed" or _aware(row.updated_at) >= now - timedelta(days=7)
            for row in existing
        )
        if occupied >= settings.issues_max_per_week:
            return None
        since = now - timedelta(days=settings.issues_window_days)
        known = {row.cause_key for row in existing}
        for cause, defects, runs in aggregate_causes(session, since):
            if (
                cause in known
                or cause not in _PROPOSALS
                or defects < settings.issues_threshold_defects
                or runs < settings.issues_threshold_runs
            ):
                continue
            marker = f"<!-- kg-audit-cause: {cause} -->"
            payload = {
                "title": f"knowledge: {cause.replace('_', ' ')} (kg-audit)",
                "body": _body(session, cause, defects, runs, since),
            }
            session.add_all(
                [
                    AuditProcessIssue(
                        cause_key=cause,
                        marker=marker,
                        defect_count=defects,
                        run_count=runs,
                        created_at=now,
                        updated_at=now,
                    )
                ]
            )
            session.commit()
            return cause, marker, payload
    return None


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _settle(engine, cause: str, number: int | None, now: datetime) -> None:
    with Session(engine) as session:
        # The conditional update also fences late failed searches on SQLite,
        # where SELECT FOR UPDATE would not lock the observed row.
        session.execute(
            update(AuditProcessIssue)
            .where(
                AuditProcessIssue.cause_key == cause, AuditProcessIssue.state != "filed"
            )
            .values(
                state="filed" if number is not None else "unresolved",
                issue_number=number,
                updated_at=now,
            )
        )
        session.commit()


def file_process_issues(*, engine=None, now: datetime | None = None) -> int:
    """Best-effort feedback after scheduled apply, outside its claim transaction."""
    if not audit_enabled():
        return 0
    try:
        settings = audit_settings()
        if not settings.issues_enabled:
            return 0
        repo = GITHUB_REPO
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("invalid audit issue repository")
        if engine is None:
            from core.db import get_engine

            engine = get_engine()
        now = _aware(now or datetime.now(timezone.utc))
        filed = 0
        with Session(engine) as session:
            pending = [
                (row.cause_key, row.marker)
                for row in session.exec(
                    select(AuditProcessIssue).where(AuditProcessIssue.state != "filed")
                ).all()
            ]
        for cause, marker in pending:
            try:
                number = _find_issue(repo, marker, cause)
                _settle(engine, cause, number, now)
                filed += number is not None
            except Exception:
                _LOGGER.warning(
                    "KG audit issue reconciliation unavailable", exc_info=True
                )
        for _ in range(settings.issues_max_per_week):
            if not audit_enabled() or not audit_settings().issues_enabled:
                break
            claim = _claim(engine, now, settings)
            if claim is None:
                break
            cause, marker, payload = claim
            try:
                number = _find_issue(repo, marker, cause)
                if number is None:
                    # Recheck the switches after reservation and before external I/O.
                    if not audit_enabled() or not audit_settings().issues_enabled:
                        break
                    result = github_request(
                        "POST", f"/repos/{repo}/issues", payload=payload
                    )
                    number = result.get("number") if isinstance(result, dict) else None
                    if type(number) is not int or number <= 0:
                        raise ValueError("GitHub audit create returned no issue number")
                _settle(engine, cause, number, now)
                filed += 1
            except Exception:
                # The durable claim survives a crash or a lost HTTP response.
                _LOGGER.warning(
                    "KG audit issue create outcome unconfirmed", exc_info=True
                )
                _settle(engine, cause, None, now)
        return filed
    except Exception:
        _LOGGER.warning("KG audit process feedback unavailable", exc_info=True)
        return 0
