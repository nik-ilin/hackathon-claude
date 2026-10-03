"""Throttled access to the Bazaar API.

Every request from the agent goes through `Api`: a token bucket keeps us under the
5 req/s per-key limit (we budget 4), and GETs survive network blips with backoff.
Writes are never retried blindly (the SDK already refuses to); the caller reconciles.
"""
from __future__ import annotations

import os
import random
import sys
import threading
import time

# a pristine copy of the official kit SDK: the live agent must not depend on edits made elsewhere
from ._sdk import Bazaar, BazaarError, Broker  # noqa: E402

URL = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai")


class TokenBucket:
    def __init__(self, rate: float, burst: int):
        self.rate, self.capacity = rate, burst
        self.tokens, self.t = float(burst), time.monotonic()
        self.lock = threading.Lock()

    def take(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.t) * self.rate)
                self.t = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            time.sleep(wait)


class Api:
    """Wraps the SDK client: same method names, throttled, GETs retried on network errors."""

    READS = {"health", "clock", "catalog", "leaderboard", "feed", "schedule", "dealers", "dealer",
             "levels", "venues", "board", "card", "me", "value", "my_threads", "my_offers",
             "thread", "duels"}

    def __init__(self, key: str, rate: float = 4.0, burst: int = 8):
        self.b = Bazaar(URL, key, wait_on_tick=False, retries=2)
        self.bucket = TokenBucket(rate, burst)
        self.network_down_since: float | None = None

    def __getattr__(self, name):
        fn = getattr(self.b, name)
        if not callable(fn):
            return fn
        is_read = name in self.READS

        def wrapped(*a, **kw):
            delay = 1.0
            while True:
                self.bucket.take()
                try:
                    out = fn(*a, **kw)
                    self.network_down_since = None
                    return out
                except BazaarError as e:
                    if e.code == "network" and is_read:
                        if self.network_down_since is None:
                            self.network_down_since = time.time()
                        time.sleep(delay + random.random() * 0.5)
                        delay = min(30.0, delay * 2)
                        if time.time() - self.network_down_since > 120:
                            raise
                        continue
                    raise
        return wrapped


def team_key() -> str:
    key = os.environ.get("BAZAAR_KEY", "").strip()
    if not key:
        raise SystemExit("BAZAAR_KEY is not set (export it; never hard-code it)")
    return key


def broker_client(broker_key: str) -> Broker:
    return Broker(URL, broker_key, retries=2)


__all__ = ["Api", "BazaarError", "team_key", "broker_client", "URL"]
