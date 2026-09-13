from __future__ import annotations

from shared.invocation_outcomes import UNKNOWN_INVOCATION as UNKNOWN_INVOCATION

# Keep this legacy node key aligned with the registered routine-job kind so
# in-flight and historical drain sessions remain correlated after the Luna
# runtime switch.
DRAINER_NODE_KEY = "qwen-drain"
KG_NODE_KEY = "kg-drain"

# Retired synthetic sessions are not operator work, so console queries keep
# using their origin marker and original prompt to hide existing database rows.
SYNTHETIC_SESSION_PREFIX = "synthetic:"
LEGACY_QWEN_SYNTHETIC_PROMPT = "Reply with exactly: qwen synthetic ok"
CODEX_SYNTHETIC_PROMPT = "Reply with exactly: codex synthetic ok"
SPARK_SYNTHETIC_PROMPT = "Reply with exactly: spark synthetic ok"

# Terminal reasons that mean the turn ended normally. The claude lane reports
# "completed" or "end_turn"; the pi lane passes the model's raw stopReason
# through, which is "stop" for a normal spark turn (see runtimes/claude/shim.py).
# None and unrecognized values remain warnings.
CLEAN_TERMINAL_REASONS = {"completed", "end_turn", "stop"}

# A durable record of an attempt that did not finish. The pending message with
# the same sequence remains live and will replace this turn after re-dispatch.
INTERRUPTED_TERMINAL_REASONS = {"interrupted"}

UNKNOWN_INVOCATION_MESSAGE = (
    "This session has an unknown invocation outcome. Reconcile the guest and any "
    "remote side effects, then start a new session. Sending again cannot resume it."
)

# A physical invoke whose synchronous response was lost while its guest kept
# working. The turn did not end, so this marker is INTERRUPTED rather than
# terminal: the pending row keeps its claim, the permit is not released, and
# the committed result receipt is adopted instead of the turn being executed a
# second time (#5938, #4322). A hold that produces no receipt inside its bound
# falls back to the ordinary unknown-outcome settlement.
RESPONSE_LOST = "response_lost"

# The agent workload runtime backstop (twelve hours, the same ceiling
# INVOKE_READ_TIMEOUT and MAX_PIN_TIMEOUT_SECONDS are sized against). A
# response-lost hold can never outlive it, whatever a node's own turn timeout
# says, so an unrecoverable hold cannot pin an admission slot indefinitely.
RESPONSE_LOST_BACKSTOP_SECONDS = 12 * 60 * 60
