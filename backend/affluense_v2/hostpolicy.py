"""Per-host concurrency and rate, replacing V1's single global delay.

V1 gates every request on `DEFAULT_DELAY = 1.0` seconds per netloc
(`affluense/http.py:_wait`). That is one policy for four very different kinds
of counterparty, and because the whole news stage talks to exactly two
hostnames, it caps the run at two requests per second no matter how many
workers are running.

Politeness is owed per host, so it is set per host:

  aggregators     news.google.com and bing.com publish keyless RSS built for
                  machine consumption. Six concurrent is ordinary use.
  WMF             Wikipedia and Wikidata ask for restraint, and V2 sends them
                  few requests anyway. Kept deliberately low.
  paid APIs       OpenAI and Firecrawl have contracts and their own documented
                  limits. There is no politeness argument for a 1s gate.
  publishers      small sites, fetched for article bodies. ONE at a time per
                  domain, exactly as strict as V1 -- concurrency comes from
                  fetching *different* publishers at once, never from
                  hammering one.

A 429 is treated as the host telling us the number was wrong: that host's rate
is halved for the rest of the run and the run says so in its notes.
"""

from __future__ import annotations

import asyncio
import time
import urllib.parse
from dataclasses import dataclass


@dataclass(frozen=True)
class Policy:
    """How hard one host may be pushed."""

    concurrency: int
    rate: float          # requests per second, sustained
    burst: float = 2.0   # bucket capacity, in requests


# Longest matching fragment wins, exactly as cache.ttl_for does it.
POLICIES: dict = {
    # Aggregator feeds: keyless, public, built to be polled.
    "news.google.com": Policy(concurrency=6, rate=5.0, burst=6),
    "bing.com": Policy(concurrency=6, rate=5.0, burst=6),

    # Wikimedia. Low volume here; kept gentle on principle.
    "wikipedia.org": Policy(concurrency=2, rate=2.0, burst=3),
    "wikidata.org": Policy(concurrency=2, rate=2.0, burst=3),
    "query.wikidata.org": Policy(concurrency=2, rate=2.0, burst=2),

    # Paid APIs with their own server-side limits.
    "api.openai.com": Policy(concurrency=8, rate=8.0, burst=8),
    "api.firecrawl.dev": Policy(concurrency=4, rate=4.0, burst=4),

    # Free metadata endpoints.
    "api.duckduckgo.com": Policy(concurrency=2, rate=2.0, burst=2),
    "open.er-api.com": Policy(concurrency=1, rate=1.0, burst=1),
}

# Everything else -- overwhelmingly individual publishers during full-text
# retrieval. One request at a time per domain, one per second. Identical
# politeness to V1; the speedup comes from many domains at once.
DEFAULT = Policy(concurrency=1, rate=1.0, burst=1)


def host_of(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc.lower()


def policy_for(url_or_host: str) -> Policy:
    host = url_or_host if "/" not in url_or_host else host_of(url_or_host)
    host = host.lower()
    best, best_len = DEFAULT, -1
    for fragment, policy in POLICIES.items():
        if (host == fragment or host.endswith("." + fragment)
                or fragment in host) and len(fragment) > best_len:
            best, best_len = policy, len(fragment)
    return best


class TokenBucket:
    """Sustained rate with a small burst, asyncio-native.

    The lock is never held across the sleep: holding it would serialise every
    waiter behind the first one and turn a rate limit into a queue.
    """

    __slots__ = ("rate", "capacity", "_tokens", "_updated", "_lock")

    def __init__(self, rate: float, capacity: float):
        self.rate = max(rate, 0.01)
        self.capacity = max(capacity, 1.0)
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity,
                    self._tokens + (now - self._updated) * self.rate,
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
            await asyncio.sleep(min(wait, 5.0))

    def penalise(self, factor: float = 0.5) -> None:
        """The host pushed back. Slow down for the rest of the run."""
        self.rate = max(self.rate * factor, 0.2)


class HostGate:
    """Concurrency semaphore plus token bucket for one host."""

    __slots__ = ("host", "policy", "_sem", "_bucket", "penalised")

    def __init__(self, host: str, policy: Policy):
        self.host = host
        self.policy = policy
        self._sem = asyncio.Semaphore(policy.concurrency)
        self._bucket = TokenBucket(policy.rate, policy.burst)
        self.penalised = False

    async def __aenter__(self):
        await self._sem.acquire()
        try:
            await self._bucket.acquire()
        except BaseException:
            self._sem.release()
            raise
        return self

    async def __aexit__(self, *exc) -> bool:
        self._sem.release()
        return False

    def penalise(self) -> bool:
        """True the first time, so the caller notes it exactly once."""
        first = not self.penalised
        self.penalised = True
        self._bucket.penalise()
        return first


class GateRegistry:
    """One gate per host, created on first use.

    Gates hold asyncio primitives, so the registry must be built inside the
    running loop -- which it is, because the transport creates it in run_async.
    """

    def __init__(self):
        self._gates: dict = {}

    def gate(self, url: str) -> HostGate:
        host = host_of(url)
        gate = self._gates.get(host)
        if gate is None:
            gate = HostGate(host, policy_for(host))
            self._gates[host] = gate
        return gate

    def describe(self) -> list:
        return sorted(
            (
                {
                    "host": g.host,
                    "concurrency": g.policy.concurrency,
                    "rate": round(g._bucket.rate, 2),
                    "penalised": g.penalised,
                }
                for g in self._gates.values()
            ),
            key=lambda r: r["host"],
        )
