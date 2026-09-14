"""Run-scoped budgets.

V1's budgets are per *instance*. `extract.analyse` constructs a fresh
`OpenAIClient(budget=OPENAI_MAX_EXTRACT_CALLS)` on every call
(`affluense/enrich/extract.py:214`), so its real ceiling is 20 calls x the
number of companies -- not the "one extraction call per company plus the
person" its comment claims. `cluster._resolve_contradiction` does the same per
*finding*.

At one request per second that is merely surprising. With eight calls in flight
it is a spending risk, so V2 makes every budget belong to the run.

Two rules hold this together:

  claim before work   A budget is claimed in deterministic order, before any
                      fan-out. Two runs over the same evidence therefore spend
                      on the same items, whatever order the network answers in.

  exhaustion degrades Running out is never an error. The caller falls back to
                      the deterministic path exactly as it does when no API key
                      is set, and the run says so in its notes.
"""

from __future__ import annotations

import asyncio
import threading


class Budget:
    """A counter with a ceiling, safe from the loop and from worker threads.

    A plain `asyncio.Lock` would not do: V1 code reached through the bridge
    claims from inside `asyncio.to_thread`, so the lock has to be a threading
    one. Claims are short and uncontended, so this never blocks the loop for a
    measurable time.
    """

    __slots__ = ("name", "limit", "used", "refused", "_lock")

    def __init__(self, name: str, limit: int):
        self.name = name
        # 0 means unlimited, matching how the env knobs read.
        self.limit = max(0, int(limit))
        self.used = 0
        self.refused = 0
        self._lock = threading.Lock()

    @property
    def unlimited(self) -> bool:
        return self.limit == 0

    @property
    def remaining(self) -> int:
        if self.unlimited:
            return 1 << 30
        with self._lock:
            return max(0, self.limit - self.used)

    def claim(self, count: int = 1) -> int:
        """Take up to `count`. Returns how many were actually granted."""
        if count <= 0:
            return 0
        with self._lock:
            if self.unlimited:
                self.used += count
                return count
            available = max(0, self.limit - self.used)
            granted = min(count, available)
            self.used += granted
            if granted < count:
                self.refused += count - granted
            return granted

    def release(self, count: int = 1) -> None:
        """Hand back what was claimed but not spent (a skipped fetch)."""
        if count <= 0:
            return
        with self._lock:
            self.used = max(0, self.used - count)

    def summary(self) -> dict:
        with self._lock:
            return {
                "name": self.name,
                "limit": self.limit or None,
                "used": self.used,
                "refused": self.refused,
            }


class RunBudgets:
    """Every ceiling one run may spend against.

    `openai_calls` is the safety net and is enforced inside the transport, so it
    binds every call site -- including V1 modules reached through the bridge,
    which build their own clients and know nothing about V2.

    `openai_extract` is sized from the work actually queued rather than fixed in
    advance: every article that V1 would have sent to the model still gets sent.
    The ceiling bounds a loop bug; it does not trim coverage.
    """

    def __init__(self, openai_calls: int, fulltext: int, peers: int = 0):
        self.openai_calls = Budget("openai.calls", openai_calls)
        self.openai_extract = Budget("openai.extract", 0)   # sized at dispatch
        # PS2's news path reads names out of headlines. Bounded separately so a
        # wide industry fan-out can never consume the screening's allowance.
        self.openai_peers = Budget("openai.peers", peers)
        self.fulltext = Budget("fulltext.fetches", fulltext)
        self.notes: list = []
        self._lock = threading.Lock()

    def size_extraction(self, batches_needed: int, headroom: int) -> None:
        """Called once, after the evidence set is known and deduped."""
        self.openai_extract.limit = max(0, batches_needed + headroom)

    def note_once(self, message: str) -> bool:
        with self._lock:
            if message in self.notes:
                return False
            self.notes.append(message)
            return True

    def summary(self) -> dict:
        return {
            "openai_calls": self.openai_calls.summary(),
            "openai_extract": self.openai_extract.summary(),
            "openai_peers": self.openai_peers.summary(),
            "fulltext": self.fulltext.summary(),
        }
