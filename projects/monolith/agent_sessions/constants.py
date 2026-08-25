from __future__ import annotations

# This session node key is deliberately independent of configurable DRAINER_JOB_KIND.
DRAINER_NODE_KEY = "qwen-drain"

# Terminal reasons that mean the turn ended normally. The claude lane reports
# "completed" or "end_turn"; the pi lane passes the model's raw stopReason
# through, which is "stop" for a normal qwen turn (see runtimes/claude/shim.py).
# None and unrecognized values remain warnings.
CLEAN_TERMINAL_REASONS = {"completed", "end_turn", "stop"}
