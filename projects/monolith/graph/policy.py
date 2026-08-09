from __future__ import annotations


def work_branch(workflow_id: str) -> str:
    """The scratch branch one graph run pushes its implementation to.

    Lives under `claude/` because that is this repo's established agent branch
    namespace (ADR 038 decision 6 has implementer runs pushing `claude/*`, and
    origin already carries a dozen). A namespace agents are not expected to
    write would make the oracle silently return "no push" forever, which is the
    same class of dead signal that AgentTurn.commit_sha turned out to be.
    """
    return f"claude/graph-{workflow_id}"


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
    )
