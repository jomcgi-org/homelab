from bench.judge import judge_free_text, JudgeConfig


def fake_caller(prompt: str) -> str:
    # deterministic stub: returns PASS iff the candidate contains "feat("
    return "PASS" if "feat(" in prompt else "FAIL"


def test_judge_passes_conventional_commit():
    cfg = JudgeConfig(
        judge_model="anthropic/claude-sonnet-4.6",
        criteria=["conventional", "no-em-dash"],
    )
    r = judge_free_text(
        candidate="feat(x): do a thing",
        task_prompt="write a commit",
        cfg=cfg,
        caller=fake_caller,
    )
    assert r.passed


def test_judge_refuses_to_grade_own_output():
    import pytest

    cfg = JudgeConfig(judge_model="anthropic/claude-sonnet-4.6", criteria=["x"])
    with pytest.raises(ValueError):
        judge_free_text(
            candidate="c",
            task_prompt="p",
            cfg=cfg,
            caller=fake_caller,
            candidate_model="anthropic/claude-sonnet-4.6",
        )
