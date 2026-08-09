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
        "\nEnd your reply with a final line exactly one of:\n"
        "VERDICT: APPROVE\nVERDICT: REQUEST_CHANGES\nVERDICT: BLOCKED"
    )


def parse_review_verdict(text: str | None) -> str:
    """Parse a review verdict from the final non-empty reply line."""
    if not text:
        return "unparseable"
    final_line = next(
        (line.strip() for line in reversed(text.splitlines()) if line.strip()), None
    )
    if final_line is None:
        return "unparseable"
    match = re.fullmatch(
        r"VERDICT:\s+(APPROVE|REQUEST_CHANGES|BLOCKED)",
        final_line,
        flags=re.IGNORECASE,
    )
    return match.group(1).lower() if match else "unparseable"
