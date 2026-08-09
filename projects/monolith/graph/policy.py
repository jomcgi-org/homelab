from __future__ import annotations

import re


def work_branch(workflow_id: str) -> str:
    """The scratch branch one graph run pushes its implementation to.

    Lives under `claude/` because that is this repo's established agent branch
    namespace (ADR 038 decision 6 has implementer runs pushing `claude/*`, and
    origin already carries a dozen). A namespace agents are not expected to
    write would make the oracle silently return "no push" forever, which is the
    same class of dead signal that AgentTurn.commit_sha turned out to be.
    """
    return f"claude/graph-{workflow_id}"


def next_action(
    attempt: int,
    max_attempts: int,
    head_sha: str | None,
    prior_sha: str | None,
) -> str:
    """Decide what the graph does next, given one attempt's outcome.

    Success means THIS attempt moved the remote branch, not that a commit
    exists. Per ADR 038 decision 1 the session's own account of itself is a
    claim, so routing keys off an artifact instead. An empty string is not a
    branch head.
    """
    if head_sha and head_sha != prior_sha:
        return "review"
    if attempt < max_attempts:
        return "retry"
    return "escalate"


def implementer_prompt(task: str, branch: str, previous_failure: str | None) -> str:
    prompt = (
        f"Implement this task: {task}\n"
        f"Create branch {branch} from the base branch, make the change, commit it, "
        f"and push {branch} to GitHub. Do not open a pull request. Do not push to main."
    )
    if previous_failure:
        prompt += f"\nPrevious attempt failed: {previous_failure}"
    return prompt


def reviewer_prompt(task: str, branch: str, commit_sha: str) -> str:
    return (
        f"Review the pushed branch {branch} for this task: {task}\n"
        f"Branch: {branch}\nCommit: {commit_sha}"
        "\nEnd your reply with a verdict on its own final line, plain text, "
        "no markdown formatting and no trailing punctuation, exactly one of:\n"
        "VERDICT: APPROVE\nVERDICT: REQUEST_CHANGES\nVERDICT: BLOCKED"
    )


# Markdown-writing reviewers decorate the verdict line (bold, bullets,
# headings) or append a closing code fence after it, so matching only a bare
# final line turns fail-closed into fail-always. Scan a short tail window and
# strip decoration before matching; anything beyond that stays unparseable.
_VERDICT_TAIL_LINES = 3
_VERDICT_PATTERN = re.compile(
    r"VERDICT:\s*(APPROVE|REQUEST_CHANGES|BLOCKED)", flags=re.IGNORECASE
)


def parse_review_verdict(text: str | None) -> str:
    """Parse a review verdict from the last few non-empty reply lines.

    Fail closed: no match, or conflicting matches within the tail window,
    returns "unparseable"; routing must never guess a verdict.
    """
    if not text:
        return "unparseable"
    tail = [line.strip() for line in text.splitlines() if line.strip()]
    verdicts = []
    for line in tail[-_VERDICT_TAIL_LINES:]:
        stripped = line.strip("*#->` \t").rstrip(".!:")
        match = _VERDICT_PATTERN.fullmatch(stripped.strip())
        if match:
            verdicts.append(match.group(1).lower())
    if len(set(verdicts)) != 1:
        return "unparseable"
    return verdicts[0]
