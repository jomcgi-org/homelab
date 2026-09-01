from __future__ import annotations

# Keep this legacy node key aligned with the registered routine-job kind so
# in-flight and historical drain sessions remain correlated after the Luna
# runtime switch.
DRAINER_NODE_KEY = "qwen-drain"

# Retired synthetic sessions are not operator work, so console queries keep
# using their origin marker and original prompt to hide existing database rows.
SYNTHETIC_SESSION_PREFIX = "synthetic:"
LEGACY_QWEN_SYNTHETIC_PROMPT = "Reply with exactly: qwen synthetic ok"

# Terminal reasons that mean the turn ended normally. The claude lane reports
# "completed" or "end_turn"; the pi lane passes the model's raw stopReason
# through, which is "stop" for a normal qwen turn (see runtimes/claude/shim.py).
# None and unrecognized values remain warnings.
CLEAN_TERMINAL_REASONS = {"completed", "end_turn", "stop"}
