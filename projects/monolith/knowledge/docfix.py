"""Prompt templates and scheduling for documentation drift fixes."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
from sqlalchemy import text
from sqlmodel import Session

from shared.invocation_outcomes import UNKNOWN_INVOCATION
from core.github import GITHUB_API, GITHUB_REPO

DOCFIX_REVIEW_TEMPLATE_VERSION = "docfix-review/luna@v1"
DOCFIX_ALLOWED_PATH_GLOBS = ("docs/**", "**/README.md", "**/ARCHITECTURE.md")
DOCFIX_PROTECTED_PATH_GLOBS = (
    ".claude/**",
    "AGENTS.md",
    "**/skills/**",
    "docs/runbooks/**",
    "docs/decisions/**",
)
DOCFIX_REVIEW_JOB_PREFIX = "docfix-review:"
DOCFIX_REVIEW_DEBOUNCE_SECONDS = 1800
DOCFIX_REVIEW_MAX_PRS = 10

logger = logging.getLogger(__name__)


def _glob_list(values: tuple[str, ...]) -> str:
    return ", ".join(f"`{value}`" for value in values)


def build_docfix_prompt(
    *,
    doc_path: str,
    doc_claim: str,
    fact_title: str,
    fact_note_id: str,
    evidence: list[str],
    suggested_fix: str,
    base_sha: str,
    head_sha: str,
    hash16: str,
) -> str:
    """Render one bounded documentation-only PR task."""
    evidence_text = "\n".join(f"- {item}" for item in evidence) or "- none supplied"
    short_title = fact_title.strip()[:60]
    allowed = _glob_list(DOCFIX_ALLOWED_PATH_GLOBS)
    protected = _glob_list(DOCFIX_PROTECTED_PATH_GLOBS)
    return f"""Fix one stale documentation claim. Never use em dashes.

Document: {doc_path}
Stale claim: {doc_claim}
Contradicting fact: {fact_title}
Fact note id: {fact_note_id}
Commit range: {base_sha}..{head_sha}
Evidence:
{evidence_text}
Suggested fix: {suggested_fix}

Make the minimal edit to {doc_path} that corrects this claim. Do not change code
or any other documentation. Do not edit generated manifests. Verify the claim
and evidence against the checkout before editing.
Documentation paths eligible for automated review match {allowed}. The protected
set is {protected}; changes there always require human merge.

Use branch `docfix/{hash16}`. Configure a qwen-drainer git identity, then commit
with a Conventional Commit `docs:` message. Before pushing, run `git diff --stat`
and stop if any path except {doc_path} changed. Set the push remote with:
`git remote set-url --push origin https://github.com/jomcgi-org/homelab.git`.
Run `export GH_TOKEN=placeholder` before any `gh` command. Push the branch and
open a PR titled `docs: {short_title}` with label `qwen-agent-for-review`. The PR
body must cite fact note id `{fact_note_id}`, the commit range, and the evidence
above. Never enable auto-merge. Return only the PR URL when successful."""


def render_docfix_review_prompt(pr_numbers: list[int], auto_merge: bool) -> str:
    """Render one bounded review turn with its exact PR set and merge policy."""
    numbers = list(dict.fromkeys(pr_numbers))
    if not numbers or len(numbers) > DOCFIX_REVIEW_MAX_PRS:
        raise ValueError("docfix review requires between 1 and 10 PR numbers")
    if any(
        not isinstance(number, int) or isinstance(number, bool) or number <= 0
        for number in numbers
    ):
        raise ValueError("docfix review PR numbers must be positive integers")

    number_text = ", ".join(str(number) for number in numbers)
    allowed = _glob_list(DOCFIX_ALLOWED_PATH_GLOBS)
    protected = _glob_list(DOCFIX_PROTECTED_PATH_GLOBS)
    if auto_merge:
        decision = (
            "When `AUTO_MERGE` is true and all gates pass: run "
            "`gh pr merge <n> --auto --rebase` and "
            "comment `docfix-review: verified against main <sha7>, queued`."
        )
    else:
        decision = (
            "When `AUTO_MERGE` is false and all gates pass: comment "
            "`docfix-review: would merge (verified against main <sha7>)` and add "
            "label `docfix-verified`."
        )

    return f"""You are reviewing documentation pull requests opened by the docfix lane.
Setup: `export GH_TOKEN=placeholder` (the proxy attaches the real credential),
`git -C /workspace/src fetch origin main`. Set `AUTO_MERGE={str(auto_merge).lower()}`.
Create the result labels idempotently before review:
`gh label create needs-human --repo jomcgi-org/homelab || true`
`gh label create docfix-verified --repo jomcgi-org/homelab || true`

Review exactly these PRs: {number_text} (at most 10). For each, run
`gh pr view <n> --repo jomcgi-org/homelab --json number,title,files,headRefName,statusCheckRollup,labels,body`,
then in order:

1. **Scope gate.** Every changed path must match {allowed}, and none may match
   {protected}. Otherwise comment
   `docfix-review: out of scope for auto-merge (<paths>)`, add label
   `needs-human`, and continue.
2. **Size gate.** Additions plus deletions are at most 60 lines. Otherwise
   comment that the change exceeds the 60-line limit, add label `needs-human`,
   and continue.
3. **Evidence gate.** Read the PR body's cited fact and evidence (file:line,
   commit). Check out `origin/main` with
   `git -C /workspace/src checkout --detach origin/main` and confirm each cited
   file still supports the new wording at HEAD by opening the file and reading
   the cited lines. If the code moved or the claim no longer holds, comment what
   differs, add label `needs-human`, and continue.
4. **Text gate.** The diff must contain no em-dash characters and must not
   delete more than it changes. Otherwise comment which text gate failed, add
   label `needs-human`, and continue. A doc rewrite is not a drift fix.
5. **CI gate.** `statusCheckRollup` must be all SUCCESS. If any check is pending,
   skip this run without comment. If a check failed, add label `needs-human` and
   continue.
6. **Decision.** {decision}
   Never use `--admin`, never squash, and never edit the PR branch. Resolve
   `<sha7>` with `git -C /workspace/src rev-parse --short=7 origin/main`.

Finish with a JSON summary block
`{{"reviewed": n, "queued": [..], "verified": [..], "needs_human": [..], "skipped_pending": [..]}}`
as the last thing in the message."""


def _review_enabled() -> bool:
    return os.environ.get("KG_DOCFIX_REVIEW_ENABLED", "false").lower() == "true"


def _auto_merge_enabled() -> bool:
    return os.environ.get("DRAINER_DOCFIX_AUTO_MERGE", "false").lower() == "true"


def schedule_docfix_review(
    session: Session, pr_numbers: list[int], delay_seconds: int
) -> bool:
    """Register a debounced, one-shot docfix review job."""
    if not _review_enabled():
        return False
    numbers = list(dict.fromkeys(pr_numbers))[:DOCFIX_REVIEW_MAX_PRS]
    if not numbers:
        return False

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=DOCFIX_REVIEW_DEBOUNCE_SECONDS)
    dialect = session.get_bind().dialect.name
    table = "routine_jobs" if dialect == "sqlite" else "claude_agent.routine_jobs"
    active = session.execute(
        text(
            f"""
            SELECT 1
              FROM {table}
             WHERE name LIKE :prefix
               AND (next_run_at IS NOT NULL
                    OR last_status = :unknown_outcome
                    OR locked_by IS NOT NULL
                    OR last_run_at >= :cutoff)
             LIMIT 1
            """
        ),
        {
            "prefix": f"{DOCFIX_REVIEW_JOB_PREFIX}%",
            "cutoff": cutoff,
            "unknown_outcome": UNKNOWN_INVOCATION,
        },
    ).first()
    if active is not None:
        return False

    payload = {
        "prompt": render_docfix_review_prompt(numbers, _auto_merge_enabled()),
        "pr_numbers": numbers,
        "repo": GITHUB_REPO,
        "branch": "main",
    }
    payload_expr = ":payload" if dialect == "sqlite" else "CAST(:payload AS JSONB)"
    result = session.execute(
        text(
            f"""
            INSERT INTO {table}
                (name, routine_kind, interval_secs, next_run_at, payload, created_by)
            VALUES
                (:name, 'qwen-drain', NULL, :next_run_at, {payload_expr}, :created_by)
            ON CONFLICT (name) DO NOTHING
            """
        ),
        {
            "name": f"{DOCFIX_REVIEW_JOB_PREFIX}{now.strftime('%Y%m%dT%H%MZ')}",
            "next_run_at": now + timedelta(seconds=max(0, delay_seconds)),
            "payload": json.dumps(payload),
            "created_by": DOCFIX_REVIEW_TEMPLATE_VERSION,
        },
    )
    session.commit()
    return result.rowcount > 0


def prune_completed_docfix_reviews(session: Session) -> int:
    """Remove completed review rows after their debounce window expires."""
    cutoff = datetime.now(timezone.utc) - timedelta(
        seconds=DOCFIX_REVIEW_DEBOUNCE_SECONDS
    )
    dialect = session.get_bind().dialect.name
    table = "routine_jobs" if dialect == "sqlite" else "claude_agent.routine_jobs"
    result = session.execute(
        text(
            f"""
            DELETE FROM {table}
             WHERE name LIKE :prefix
               AND next_run_at IS NULL
               AND locked_by IS NULL
               AND last_run_at < :cutoff
               AND (last_status IS NULL OR last_status != :unknown_outcome)
            """
        ),
        {
            "prefix": f"{DOCFIX_REVIEW_JOB_PREFIX}%",
            "cutoff": cutoff,
            "unknown_outcome": UNKNOWN_INVOCATION,
        },
    )
    session.commit()
    return result.rowcount


def _github_get(url: str) -> httpx.Response:
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(timeout=15.0, follow_redirects=True) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        return response


def find_reviewable_docfix_prs(session: Session) -> list[int]:
    """Return up to ten open docs PRs that still need docfix review."""
    del session
    if not _review_enabled():
        return []
    if not os.environ.get("GITHUB_API_TOKEN"):
        logger.warning(
            "docfix-review sweep unavailable without GITHUB_API_TOKEN; "
            "completion-triggered review remains available"
        )
        return []

    query = urlencode({"state": "open", "per_page": 100})
    try:
        response = _github_get(f"{GITHUB_API}/repos/{GITHUB_REPO}/pulls?{query}")
        pulls = response.json()
        if not isinstance(pulls, list):
            raise ValueError("GitHub pull list response was not a list")
    except Exception:  # noqa: BLE001 - sweep failure must not stop extraction
        logger.warning(
            "docfix-review GitHub sweep unavailable; completion-triggered review "
            "remains available",
            exc_info=True,
        )
        return []

    reviewable = []
    for pull in pulls:
        if not isinstance(pull, dict) or not str(pull.get("title", "")).startswith(
            "docs:"
        ):
            continue
        head = pull.get("head")
        if not isinstance(head, dict) or not str(head.get("ref", "")).startswith(
            "docfix/"
        ):
            continue
        labels = {
            item.get("name")
            for item in pull.get("labels", [])
            if isinstance(item, dict)
        }
        number = pull.get("number")
        if (
            "qwen-agent-for-review" in labels
            and not labels.intersection({"docfix-verified", "needs-human"})
            and isinstance(number, int)
        ):
            reviewable.append(number)
        if len(reviewable) == DOCFIX_REVIEW_MAX_PRS:
            break
    return reviewable
