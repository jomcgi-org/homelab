"""LLM-as-judge for free-text task grading.

Only used for task classes where no deterministic verifier exists (commit-message
quality, style). Criterion-order permutation is applied to mitigate position bias.
Cue-stripping (removing model-identifiable artefacts from the candidate before
judging) is a documented future hook; nothing to strip here beyond whitespace trimming.
"""

from __future__ import annotations

from typing import Callable

from pydantic import BaseModel


class JudgeConfig(BaseModel):
    judge_model: str
    criteria: list[str]
    permutations: int = 2


class JudgeResult(BaseModel):
    passed: bool
    votes: list[str]


def judge_free_text(
    *,
    candidate: str,
    task_prompt: str,
    cfg: JudgeConfig,
    caller: Callable[[str], str],
    candidate_model: str | None = None,
) -> JudgeResult:
    """Grade a free-text candidate output via majority vote over criterion-order permutations.

    Args:
        candidate: The model output to evaluate.
        task_prompt: The original task prompt the candidate was responding to.
        cfg: Judge configuration (model, criteria, number of permutation variants).
        caller: Callable that sends a prompt string to the judge LLM and returns a verdict.
        candidate_model: If provided, guard against the judge grading its own output.

    Returns:
        JudgeResult with majority-vote outcome and individual votes.

    Raises:
        ValueError: If candidate_model matches cfg.judge_model (self-preference bias guard).
    """
    # Self-preference guard: a model judging its own output inflates scores reliably.
    if candidate_model is not None and candidate_model == cfg.judge_model:
        raise ValueError(
            "judge model must differ from candidate model (self-preference bias)"
        )

    n = len(cfg.criteria)
    votes: list[str] = []

    for i in range(cfg.permutations):
        # Rotate criteria left by i positions (deterministic, reproducible).
        if n == 0:
            rotated: list[str] = []
        else:
            shift = i % n
            rotated = cfg.criteria[shift:] + cfg.criteria[:shift]

        criteria_block = "\n".join(f"{j + 1}. {c}" for j, c in enumerate(rotated))
        prompt = (
            f"You are grading a candidate output against criteria for a task.\n\n"
            f"Task prompt: {task_prompt}\n\n"
            f"Criteria (evaluate in this order):\n{criteria_block}\n\n"
            f"Candidate output:\n{candidate}\n\n"
            f"Reply with exactly PASS if the candidate satisfies all criteria, "
            f"or FAIL if it does not."
        )

        verdict = caller(prompt)
        vote = "PASS" if verdict.strip().upper().startswith("PASS") else "FAIL"
        votes.append(vote)

    passed = votes.count("PASS") > len(votes) / 2
    return JudgeResult(passed=passed, votes=votes)
