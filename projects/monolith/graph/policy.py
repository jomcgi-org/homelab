from __future__ import annotations


def next_action(attempt: int, max_attempts: int, commit_sha: str | None) -> str:
    """Decide what the graph does next, given one attempt's outcome.

    Success is "a commit exists", never a reading of the agent's prose. Per ADR
    038 decision 1 the session's own account of itself is a claim, so routing
    keys off an artifact instead. An empty string is not a commit.
    """
    if commit_sha:
        return "review"
    if attempt < max_attempts:
        return "retry"
    return "escalate"


def implementer_prompt(task: str, previous_failure: str | None) -> str:
    prompt = f"Implement this task: {task}"
    if previous_failure:
        prompt += f"\nPrevious attempt failed: {previous_failure}"
    return prompt


def reviewer_prompt(task: str, branch: str, commit_sha: str) -> str:
    return (
        f"Review the implementation of: {task}\nBranch: {branch}\nCommit: {commit_sha}"
    )
