import pytest

from graph.policy import (
    implementer_prompt,
    next_action,
    parse_review_verdict,
    reviewer_prompt,
    work_branch,
)


def test_work_branch():
    assert work_branch("wf-123") == "claude/graph-wf-123"
    # Must live in the agent branch namespace agents can actually push to.
    assert work_branch("wf-123").startswith("claude/")


@pytest.mark.parametrize(
    ("attempt", "maximum", "head", "prior", "expected"),
    [
        (
            attempt,
            maximum,
            head,
            prior,
            "review"
            if head and head != prior
            else "retry"
            if attempt < maximum
            else "escalate",
        )
        for maximum in (1, 2, 3)
        for attempt in (0, 1, 2, 3, 4)
        for head in (None, "", "abc", "def")
        for prior in (None, "abc", "def")
    ],
)
def test_next_action_is_exhaustive(attempt, maximum, head, prior, expected):
    assert next_action(attempt, maximum, head, prior) == expected


def test_next_action_rejects_stale_head_after_failed_attempt():
    assert next_action(2, 3, "sha1", "sha1") == "retry"
    assert next_action(2, 2, "sha1", "sha1") == "escalate"


@pytest.mark.parametrize(("prior", "head"), [(None, "sha"), ("sha1", "sha2")])
def test_next_action_routes_new_head_to_review(prior, head):
    assert next_action(2, 3, head, prior) == "review"


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
    assert prompt.endswith(
        "VERDICT: APPROVE\nVERDICT: REQUEST_CHANGES\nVERDICT: BLOCKED"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, "unparseable"),
        ("", "unparseable"),
        ("no verdict", "unparseable"),
        ("VERDICT: APPROVE", "approve"),
        ("  verdict: request_changes  \n", "request_changes"),
        ("\n VERDICT: blocked \n", "blocked"),
        ("VERDICT: MAYBE", "unparseable"),
    ],
)
def test_parse_review_verdict(text, expected):
    assert parse_review_verdict(text) == expected


def test_parse_review_verdict_uses_clean_final_line():
    text = (
        "The review discusses VERDICT: BLOCKED as a possibility.\n\nVERDICT: APPROVE\n"
    )
    assert parse_review_verdict(text) == "approve"


def test_parse_review_verdict_rejects_conflicting_final_lines():
    assert parse_review_verdict("VERDICT: APPROVE VERDICT: BLOCKED") == "unparseable"
