"""Guard that Pi's per-session windows fit NInfer's shared KV pool.

NInfer dynamically shares one KV page pool across its generation lanes. Pi's
advertised context multiplied by that lane count, plus shared headroom for
estimation error, must fit the pool. This test reads the deployment values so
a server-side capacity or concurrency change cannot silently break the budget.
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
    """All concurrent Pi windows plus headroom must fit the shared KV pool.

    Pi's chars/4 heuristic undercounted repetitive-ASCII tool results by 4097
    tokens in production. The shared gap protects both active lanes from that
    estimation error while preserving the full server window for a lone direct
    API request.
    """
    import yaml

    values_path = _repo_path("projects", "inference", "deploy", "values.yaml")

    with open(values_path) as stream:
        config = yaml.safe_load(stream)

    ninfer = config.get("ninfer", {})
    max_context = ninfer.get("maxContext")
    kv_capacity = ninfer.get("kvCapacity")
    max_concurrency = ninfer.get("maxConcurrency")
    assert max_context is not None, "ninfer.maxContext not found in values.yaml"
    assert kv_capacity is not None, "ninfer.kvCapacity not found in values.yaml"
    assert max_concurrency is not None, "ninfer.maxConcurrency not found in values.yaml"

    # A single Pi request must never exceed the server's per-request ceiling.
    assert shim.PI_CONTEXT_WINDOW <= max_context, (
        "PI_CONTEXT_WINDOW (%s) exceeds ninfer.maxContext (%s). "
        "Lower PI_CONTEXT_WINDOW in projects/embervm/runtimes/claude/shim.py or "
        "raise ninfer.maxContext in projects/inference/deploy/values.yaml."
        % (shim.PI_CONTEXT_WINDOW, max_context)
    )

    # Every generation lane may host a full Pi session concurrently.
    allocated = shim.PI_CONTEXT_WINDOW * max_concurrency
    actual_headroom = kv_capacity - allocated
    assert actual_headroom >= shim.PI_CONTEXT_WINDOW_HEADROOM, (
        "%s Pi windows allocate %s of %s KV tokens, leaving %s headroom. "
        "This falls below PI_CONTEXT_WINDOW_HEADROOM (%s). "
        "Lower PI_CONTEXT_WINDOW in shim.py to widen the gap, or lower "
        "PI_CONTEXT_WINDOW_HEADROOM if a smaller margin is genuinely justified. "
        "The gap must absorb pi's estimate error, which measured at ~4097 tokens "
        "for repetitive ASCII tool results."
        % (
            max_concurrency,
            allocated,
            kv_capacity,
            actual_headroom,
            shim.PI_CONTEXT_WINDOW_HEADROOM,
        )
    )
