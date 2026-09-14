"""Structured progress, and cooperative cancellation.

V1 reports progress by printing lines, and `api.py` works out how far along a
run is by regexing them (`_PHASES`, `_SCREENING_COMPANY`). That cannot survive
concurrency: with nine evidence agents in flight, "Screening: X" lines arrive
interleaved and counting them says nothing about elapsed work.

So V2 reports twice. It still emits a human line for the run log, which keeps
the existing `<RunStatus>` view working unchanged, and it emits a structured
event the API stores verbatim. V1's string-matching path is never touched.

Cancellation is cooperative for the same reason it is in V1: there is no safe
way to kill work mid-request. V2 checks a flag before every HTTP call and at
every stage boundary, so a stop lands within a second or two and never
mid-write. The exception the host raised is preserved and re-raised at the top,
so `api.py` still sees its own `RunCancelled` and marks the job cancelled
rather than failed.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


class Cancelled(Exception):
    """Raised inside V2 when the run has been asked to stop."""


# Stage weights. Roughly proportional to measured share of a cold run, so the
# bar moves at a believable speed rather than jumping. They do not affect what
# the pipeline does -- only what the browser is told.
STAGE_WEIGHTS = (
    ("starting", 1),
    ("identity", 5),
    ("discovery", 14),
    ("evidence", 30),
    ("assignment", 2),
    ("fulltext", 16),
    ("analysis", 20),
    ("findings", 6),
    ("network", 3),
    ("report", 3),
)

STAGE_LABELS = {
    "starting": "starting",
    "identity": "resolving identity",
    "discovery": "discovering companies",
    "evidence": "gathering evidence",
    "assignment": "assigning evidence",
    "fulltext": "reading articles",
    "analysis": "analysing evidence",
    "findings": "building findings",
    "network": "mapping the network",
    "report": "assembling the report",
}

_ORDER = [name for name, _ in STAGE_WEIGHTS]
_WEIGHT = dict(STAGE_WEIGHTS)
_TOTAL_WEIGHT = sum(_WEIGHT.values())


@dataclass
class Stage:
    name: str
    done: int = 0
    total: int = 0
    complete: bool = False
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None

    @property
    def fraction(self) -> float:
        if self.complete:
            return 1.0
        if self.total <= 0:
            return 0.0
        return max(0.0, min(1.0, self.done / self.total))

    @property
    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return round(end - self.started_at, 2)


class Reporter:
    """Progress sink for one run.

    Thread-safe because V1 code reached through the bridge reports from worker
    threads while agents report from the loop.
    """

    def __init__(self, on_progress=None, on_event=None, verbose: bool = False,
                 cancel_check=None):
        self._on_progress = on_progress
        self._on_event = on_event
        self._verbose = verbose
        self._cancel_check = cancel_check
        self._lock = threading.RLock()
        self._stages: dict = {}
        self._current = "starting"
        self._started = time.monotonic()
        self._cancelled = False
        # The host's own cancellation exception, preserved so the API sees the
        # type it already knows how to report.
        self.cancel_exception: BaseException | None = None
        self.stage("starting", total=1)

    # -- cancellation -------------------------------------------------------
    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def request_cancel(self, exc: BaseException | None = None) -> None:
        with self._lock:
            self._cancelled = True
            if exc is not None and self.cancel_exception is None:
                self.cancel_exception = exc

    def check(self) -> None:
        """Raise if the run has been asked to stop. Called at every await point."""
        if self._cancelled:
            raise Cancelled()
        if self._cancel_check is not None and self._cancel_check():
            self.request_cancel()
            raise Cancelled()

    def raise_host_cancellation(self) -> None:
        """Re-raise the host's exception if it is what stopped us."""
        if self.cancel_exception is not None:
            raise self.cancel_exception

    # -- stages -------------------------------------------------------------
    def stage(self, name: str, total: int = 0) -> None:
        with self._lock:
            stage = self._stages.get(name)
            if stage is None:
                stage = Stage(name=name, total=total)
                self._stages[name] = stage
            elif total:
                stage.total = total
            self._current = name
        self._emit()

    def advance(self, name: str, by: int = 1) -> None:
        with self._lock:
            stage = self._stages.setdefault(name, Stage(name=name))
            stage.done += by
        self._emit()

    def finish(self, name: str) -> None:
        with self._lock:
            stage = self._stages.setdefault(name, Stage(name=name))
            stage.complete = True
            stage.finished_at = time.monotonic()
        self._emit()

    def skip(self, name: str) -> None:
        """A stage that will not run at all (no Firecrawl key, PS1-only run)."""
        with self._lock:
            stage = self._stages.setdefault(name, Stage(name=name))
            stage.complete = True
            stage.total = 0
            stage.finished_at = time.monotonic()
        self._emit()

    # -- output -------------------------------------------------------------
    def say(self, message: str) -> None:
        """A human line for the run log. Also the cancellation checkpoint.

        The host's callback is what raises when a job has been cancelled, so
        its exception is captured rather than swallowed: re-raised at the top
        of the run it tells `api.py` this was a stop, not a failure.
        """
        text = (message or "").strip()
        if self._verbose:
            print(message, flush=True)
        if self._on_progress is None:
            return
        try:
            self._on_progress(text)
        except BaseException as exc:      # the host's RunCancelled
            self.request_cancel(exc)
            raise Cancelled() from exc

    def _emit(self) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(self.snapshot())
        except BaseException as exc:
            self.request_cancel(exc)
            raise Cancelled() from exc

    # -- state --------------------------------------------------------------
    def percent(self) -> int:
        with self._lock:
            earned = 0.0
            for name in _ORDER:
                stage = self._stages.get(name)
                if stage is None:
                    continue
                earned += _WEIGHT[name] * stage.fraction
        # Never report 100 while work is outstanding; a full bar over a running
        # job is worse than one sitting at 97.
        return int(min(99, round(earned / _TOTAL_WEIGHT * 100)))

    def snapshot(self) -> dict:
        with self._lock:
            current = self._current
            stages = [
                {
                    "stage": s.name,
                    "done": s.done,
                    "total": s.total,
                    "complete": s.complete,
                    "elapsed_s": s.elapsed,
                }
                for s in (self._stages.get(n) for n in _ORDER)
                if s is not None
            ]
        return {
            "engine": "v2",
            "stage": current,
            "phase": STAGE_LABELS.get(current, current),
            "percent": self.percent(),
            "elapsed_s": round(time.monotonic() - self._started, 2),
            "stages": stages,
        }

    def timings(self) -> dict:
        """Per-stage wall clock, for the benchmark."""
        with self._lock:
            return {
                s.name: s.elapsed
                for s in (self._stages.get(n) for n in _ORDER)
                if s is not None
            }
