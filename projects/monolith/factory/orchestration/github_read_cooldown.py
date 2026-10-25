"""Process-shared pacing for the factory's GitHub reads, never cached evidence.

The conductor has one elected process. Its callers and threads share this clock
across repositories. A replacement process learns the provider's limits from
its first response; this is not a distributed quota ledger or execution grant.
"""

import math
import threading
import time


class GitHubReadDeferred(RuntimeError):
    """No network request or ownership observation occurred during this call."""


class GitHubReadCooldown:
    def __init__(self, *, monotonic=None, wall_time=None):
        self._monotonic = monotonic or time.monotonic
        self._wall_time = wall_time or time.time
        self._lock = threading.Lock()
        self._until = 0.0
        self._failures = 0

    def check(self):
        with self._lock:
            remaining = self._until - self._monotonic()
        if remaining > 0:
            raise GitHubReadDeferred(
                f"Factory GitHub read deferred for {math.ceil(remaining)}s after observed rate limit; ownership remains unobserved"
            )

    def observe(self, metadata):
        """Extend a deadline from sanitized headers, never shorten a known wait."""
        limited = metadata.get("remaining") == "0" or metadata.get(
            "classification"
        ) in {"primary_rate_limit", "secondary_rate_limit", "rate_limited"}
        with self._lock:
            now = self._monotonic()
            if not limited:
                if metadata.get("status") == 200 and now >= self._until:
                    self._failures = 0
                return None
            wall = self._wall_time()
            delays = []
            if metadata.get("remaining") == "0" and metadata.get("reset") is not None:
                delays.append(int(metadata["reset"]) - wall)
            if metadata.get("retry_after") is not None:
                delays.append(int(metadata["retry_after"]))
            delay = max(delays, default=0)
            if metadata.get("status") in (403, 429):
                self._failures += 1
            if delay <= 0:
                # Missing/stale headers are not permission for a tight retry.
                delay = min(3600, 60 * 2 ** min(max(0, self._failures - 1), 6))
            until = now + delay + 1
            if until <= self._until:
                return None
            self._until = until
            return {
                "retry_after_seconds": math.ceil(delay + 1),
                "retry_at_epoch": math.ceil(wall + delay + 1),
            }
