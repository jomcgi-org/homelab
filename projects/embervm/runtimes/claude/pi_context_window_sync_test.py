"""Guard that PI_CONTEXT_WINDOW stays in sync with vLLM configuration.

A stale PI_CONTEXT_WINDOW value silently re-arms the bug where pi's
clampMaxTokensToContext computes available output tokens using a false
budget. This guard verifies the constant tracks the source of truth.
"""

import os
from pathlib import Path

import shim


def _repo_path(*parts: str) -> Path:
    """Resolve a repo-relative path, in-bazel (TEST_SRCDIR) or standalone."""
    rel = Path(*parts)
    candidate = Path(os.environ.get("TEST_SRCDIR", "")) / "_main" / rel
    if candidate.exists():
        return candidate
    # Direct run: this file lives at projects/embervm/runtimes/claude/.
    here = Path(__file__).resolve().parents[4] / rel
    if here.exists():
        return here
    raise FileNotFoundError(f"{rel} not found at {candidate} or {here}")


def test_pi_context_window_matches_inference_config():
    """PI_CONTEXT_WINDOW must match vLLM's maxModelLen in inference config.

    A stale PI_CONTEXT_WINDOW re-arms the bug where pi's
    clampMaxTokensToContext computes available output tokens using a false
    budget. This test prevents silent de-synchronization by verifying the
    constant tracks the source of truth in projects/inference/deploy/values.yaml.
    """
    import yaml

    values_path = _repo_path("projects", "inference", "deploy", "values.yaml")

    with open(values_path) as stream:
        config = yaml.safe_load(stream)

    expected_window = config.get("vllm", {}).get("maxModelLen")
    assert expected_window is not None, "vllm.maxModelLen not found in values.yaml"
    assert shim.PI_CONTEXT_WINDOW == expected_window, (
        "PI_CONTEXT_WINDOW (%s) does not match inference vllm.maxModelLen (%s). "
        "Edit projects/embervm/runtimes/claude/shim.py to match, or update "
        "projects/inference/deploy/values.yaml if the vLLM setting intentionally changed."
        % (shim.PI_CONTEXT_WINDOW, expected_window)
    )
