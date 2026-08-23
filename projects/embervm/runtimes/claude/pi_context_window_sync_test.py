"""Guard that PI_CONTEXT_WINDOW and PI_CONTEXT_WINDOW_HEADROOM satisfy the invariant.

PI_CONTEXT_WINDOW is deliberately set below vLLM's --max-model-len to absorb
pi's estimate error. A too-large window (matching vLLM's real limit exactly)
silently re-arms the bug where pi's clampMaxTokensToContext computes available
output tokens using a false budget. A too-small window defeats the headroom's
purpose. This guard verifies both the upper bound and minimum headroom.
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


def test_pi_context_window_stays_under_inference_config():
    """PI_CONTEXT_WINDOW must be below the served context with minimum headroom.

    PI_CONTEXT_WINDOW is deliberately set below the server's max context to absorb
    pi's estimate error. The gap converts pi's fixed safety margin into a larger
    effective margin that accounts for the chars/4 estimation heuristic. Measured
    in prod on 2026-08-07, a turn with repetitive-ASCII tool results undercounted
    by 4097 tokens. This test ensures pi can never believe it has more room than
    the model actually provides, and that the headroom gap is sufficient.
    """
    import yaml

    values_path = _repo_path("projects", "inference", "deploy", "values.yaml")

    with open(values_path) as stream:
        config = yaml.safe_load(stream)

    # The served window is NInfer's --max-context (#5155). PI_CONTEXT_WINDOW is
    # far below it on purpose: raising pi's window to use the 262K the server
    # admits is a separate decision about per-turn cost, not a sync fix.
    max_model_len = config.get("ninfer", {}).get("maxContext")
    assert max_model_len is not None, "ninfer.maxContext not found in values.yaml"

    # (a) PI_CONTEXT_WINDOW must never exceed the model's real capacity
    assert shim.PI_CONTEXT_WINDOW <= max_model_len, (
        "PI_CONTEXT_WINDOW (%s) exceeds ninfer.maxContext (%s). "
        "Lower PI_CONTEXT_WINDOW in projects/embervm/runtimes/claude/shim.py or "
        "raise ninfer.maxContext in projects/inference/deploy/values.yaml."
        % (shim.PI_CONTEXT_WINDOW, max_model_len)
    )

    # (b) Headroom must be at least the declared minimum
    actual_headroom = max_model_len - shim.PI_CONTEXT_WINDOW
    assert actual_headroom >= shim.PI_CONTEXT_WINDOW_HEADROOM, (
        "Headroom (%s tokens) falls below PI_CONTEXT_WINDOW_HEADROOM (%s). "
        "Lower PI_CONTEXT_WINDOW in shim.py to widen the gap, or lower "
        "PI_CONTEXT_WINDOW_HEADROOM if a smaller margin is genuinely justified. "
        "The gap must absorb pi's estimate error, which measured at ~4097 tokens "
        "for repetitive ASCII tool results."
        % (actual_headroom, shim.PI_CONTEXT_WINDOW_HEADROOM)
    )
