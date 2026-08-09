import pytest

from graph.policy import implementer_prompt, next_action, reviewer_prompt, work_branch


def test_work_branch():
    assert work_branch("wf-123") == "claude/graph-wf-123"
    # Must live in the agent branch namespace agents can actually push to.
    assert work_branch("wf-123").startswith("claude/")


@pytest.mark.parametrize(
    ("attempt", "maximum", "commit", "expected"),
    [
        (
            attempt,
            maximum,
            commit,
            "review"
            if commit is not None
            else "retry"
            if attempt < maximum
            else "escalate",
        )
        for maximum in (1, 2, 3)
        for attempt in (0, 1, 2, 3, 4)
        for commit in (None, "abc")
    ],
)
def test_next_action_is_exhaustive(attempt, maximum, commit, expected):
    assert next_action(attempt, maximum, commit) == expected


def test_prompt_builders():
    prompt = implementer_prompt("fix bug", "claude/graph-wf-123", None)
    assert "claude/graph-wf-123" in prompt
    assert "Do not open a pull request" in prompt
    assert "Do not push to main" in prompt
    assert "Previous attempt failed: tests failed" in implementer_prompt(
        "fix bug", "claude/graph-wf-123", "tests failed"
    )
    prompt = reviewer_prompt("fix bug", "feature", "abc123")
    assert "fix bug" in prompt
    assert "feature" in prompt
    assert "abc123" in prompt
