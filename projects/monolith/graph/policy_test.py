import pytest

from graph.policy import implementer_prompt, next_action, reviewer_prompt


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
    assert implementer_prompt("fix bug", None) == "Implement this task: fix bug"
    assert "Previous attempt failed: tests failed" in implementer_prompt(
        "fix bug", "tests failed"
    )
    prompt = reviewer_prompt("fix bug", "feature", "abc123")
    assert "fix bug" in prompt
    assert "feature" in prompt
    assert "abc123" in prompt
