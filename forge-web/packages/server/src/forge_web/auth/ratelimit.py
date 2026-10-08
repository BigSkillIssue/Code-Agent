"""Simple sliding-window rate limits for sign-in, sign-up and reset requests."""

import time
from collections import defaultdict, deque


class RateLimiter:
    """At most `limit` hits per key within `window` seconds."""

    def __init__(self, limit: int, window: float) -> None:
        self.limit = limit
        self.window = window
        self._hits: defaultdict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        """Count a hit; False if the key is over its limit."""
        now = time.monotonic()
        hits = self._hits[key]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        if len(self._hits) > 100_000:  # forget idle keys now and then
            for stale in [k for k, v in self._hits.items() if not v or now - v[-1] > self.window]:
                del self._hits[stale]
        return True


class AuthLimits:
    """The rate limits of one server."""

    def __init__(self) -> None:
        self.login_by_ip = RateLimiter(30, 900)
        self.login_by_email = RateLimiter(10, 900)
        self.signup_by_ip = RateLimiter(10, 3600)
        self.mail_by_email = RateLimiter(3, 3600)
