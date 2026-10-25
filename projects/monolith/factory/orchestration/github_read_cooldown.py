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


class GitHubReadTelemetry:
    """Bounded process-local counters, never request contents or authority."""

    INTERVAL_SECONDS = 60
    _ROUTES = {"issues", "pulls", "commits", "check-runs"}

    def __init__(self, *, monotonic=None):
        self._monotonic = monotonic or time.monotonic
        self._lock = threading.Lock()
        self._started = self._monotonic()
        self._counts = {}
        self._quota = None
        self._provider_consumption = 0
        self._quota_samples = 0

    def observe(self, suffix=None, metadata=None, *, deferred=False):
        # Only fixed endpoint families enter the counters, not repositories,
        # object identifiers, queries, tokens, request IDs or response bodies.
        family = str(suffix or "").split("?", 1)[0].split("/", 1)[0]
        family = family if family in self._ROUTES else "other"
        metadata = metadata or {}
        with self._lock:
            key = "deferred" if deferred else f"requests_{family}"
            self._counts[key] = self._counts.get(key, 0) + 1
            if not deferred:
                status = metadata.get("status")
                category = (
                    f"responses_{status // 100}xx"
                    if isinstance(status, int) and 100 <= status < 600
                    else "without_response"
                )
                self._counts[category] = self._counts.get(category, 0) + 1
                remaining, reset = metadata.get("remaining"), metadata.get("reset")
                if (
                    isinstance(remaining, str)
                    and remaining.isascii()
                    and remaining.isdecimal()
                    and isinstance(reset, str)
                    and reset.isascii()
                    and reset.isdecimal()
                    and len(remaining) <= 12
                    and len(reset) <= 12
                ):
                    current = (int(reset), int(remaining))
                    if self._quota is not None and self._quota[0] == current[0]:
                        self._provider_consumption += max(
                            0, self._quota[1] - current[1]
                        )
                        current = (current[0], min(current[1], self._quota[1]))
                    if self._quota is None or current[0] >= self._quota[0]:
                        self._quota = current
                    self._quota_samples += 1
            now = self._monotonic()
            if now - self._started < self.INTERVAL_SECONDS:
                return None
            result = {
                "window_seconds": round(now - self._started, 3),
                "counts": self._counts,
                "quota_samples": self._quota_samples,
                # This includes every consumer sharing the provider quota.
                # It is not an attribution of that usage to the factory.
                "observed_shared_consumption": self._provider_consumption,
                "observed_remaining": self._quota[1] if self._quota_samples else None,
                "observed_reset_epoch": self._quota[0] if self._quota_samples else None,
            }
            self._counts = {}
            self._provider_consumption = self._quota_samples = 0
            self._started = now
            return result
