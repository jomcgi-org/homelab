from __future__ import annotations

import os


def enabled() -> bool:
    return os.environ.get("SWARM_ENABLED", "false").lower() == "true"


def implementer_model() -> str:
    return os.environ.get("SWARM_IMPLEMENTER_MODEL", "luna")


def reviewer_model() -> str:
    return os.environ.get("SWARM_REVIEWER_MODEL", "opus")


def max_attempts() -> int:
    return int(os.environ.get("SWARM_MAX_ATTEMPTS", "2"))


def max_review_cycles() -> int:
    return int(os.environ.get("SWARM_MAX_REVIEW_CYCLES", "2"))


def turn_timeout_seconds() -> int:
    return int(os.environ.get("SWARM_TURN_TIMEOUT_SECONDS", "1800"))


def decision_timeout_seconds() -> int:
    # Escalations wait this long for a human decision via the console, POST
    # .../decision or agent_run_decide (ADR agents/060); 0 makes escalation
    # terminal as before.
    return int(os.environ.get("SWARM_DECISION_TIMEOUT_SECONDS", "86400"))


def codex_concurrency() -> int:
    return int(os.environ.get("SWARM_CODEX_CONCURRENCY", "2"))


def factory_max_concurrent_tasks() -> int:
    # Hard ceiling on factory tasks in flight at once, applied on top of the
    # policy's max_tasks. The chart owns it so a policy posted with a larger
    # number cannot open more lanes than the platform has been sized for.
    return max(1, int(os.environ.get("FACTORY_MAX_CONCURRENT_TASKS", "1")))
