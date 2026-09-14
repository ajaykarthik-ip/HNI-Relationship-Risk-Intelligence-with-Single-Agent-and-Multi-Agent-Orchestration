"""Run V1's synchronous source modules on V2's async transport, unchanged.

`wikidata.py`, `wikipedia.py`, `web.py`, `qc.py`, `cluster.py` and
`relationships.py` all take a `fetcher` and call it synchronously. Rewriting
them async would be a large amount of work for stages that are a small share of
the runtime, and every line rewritten is a line that can disagree with V1.

So they are not rewritten. `SyncFacade` presents exactly the surface those
modules use -- `get`, `get_json`, `post_json`, `note`, `robots_allow`, `meter`,
`cache`, `notes` -- and forwards each call onto the running event loop. The V1
function is executed in a worker thread, so blocking there blocks nothing else,
and it shares V2's cache, meter, host gates and budgets as if it had been
written for them.

This is what keeps the diff small. Only the four high-volume paths -- news,
full text, OpenAI and Firecrawl -- get real async implementations, because that
is where all the time is.
"""

from __future__ import annotations

import asyncio

from . import config as v2config
from .progress import Cancelled


class SyncFacade:
    """A `Fetcher`-shaped view of the async transport, safe from a thread.

    Every method blocks the *calling thread* while the coroutine runs on the
    loop. That is only correct off the loop thread, which is guaranteed because
    the only way in is `run_v1`, and it always uses `asyncio.to_thread`.
    """

    def __init__(self, transport, loop: asyncio.AbstractEventLoop):
        self._transport = transport
        self._loop = loop
        # Mirrored so V1 code that reads them directly keeps working.
        self.meter = transport.meter
        self.cache = transport.cache
        self.delay = 0.0
        self.timeout = v2config.READ_TIMEOUT

    @property
    def notes(self) -> list:
        return self._transport.notes

    def _run(self, coro):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result()

    # -- the Fetcher surface ------------------------------------------------
    def get(self, url: str, attempts: int = 3, **kwargs):
        params = kwargs.pop("params", None)
        headers = kwargs.pop("headers", None)
        timeout = kwargs.pop("timeout", None)
        return self._run(self._transport.get(
            url, attempts=attempts, params=params, headers=headers,
            timeout=timeout,
        ))

    def get_json(self, url: str, **kwargs):
        response = self.get(url, **kwargs)
        if response is None:
            return None
        try:
            return response.json()
        except ValueError:
            self.note(f"Response from {url} was not JSON")
            return None

    def post_json(self, url: str, payload: dict, headers: dict,
                  attempts: int = 3, kind: str | None = None, units: int = 1,
                  on_response=None, timeout: float | None = None):
        return self._run(self._transport.post_json(
            url, payload, headers, attempts=attempts, kind=kind, units=units,
            on_response=on_response, timeout=timeout,
        ))

    def robots_allow(self, url: str) -> bool:
        return self._run(self._transport.robots_allow(url))

    def note(self, message: str) -> None:
        self._transport.note(message)


class Bridge:
    """Runs V1 callables in worker threads, bounded.

    The bound matters: without it a company-per-thread fan-out would size the
    default executor by accident, and the host gates -- not the thread count --
    are supposed to be what limits load.
    """

    def __init__(self, transport, loop: asyncio.AbstractEventLoop,
                 limit: int | None = None):
        self.facade = SyncFacade(transport, loop)
        self._sem = asyncio.Semaphore(limit or v2config.MAX_BRIDGE_THREADS)
        self._reporter = transport.reporter

    async def run(self, fn, *args, **kwargs):
        """Call a V1 function with the facade already bound as its fetcher."""
        self._reporter.check()
        async with self._sem:
            return await asyncio.to_thread(fn, self.facade, *args, **kwargs)

    async def run_bare(self, fn, *args, **kwargs):
        """Call a V1 function that takes no fetcher (pure, but slow enough to
        be worth keeping off the loop -- BeautifulSoup, mostly)."""
        self._reporter.check()
        async with self._sem:
            return await asyncio.to_thread(fn, *args, **kwargs)

    async def gather(self, calls: list):
        """Run several V1 calls concurrently, preserving input order.

        Order is preserved deliberately: findings and screenings are assembled
        from these lists, and letting completion order decide would make two
        runs over identical evidence disagree.

        A failure is returned in place rather than raised, so one dead source
        cannot sink the run. Cancellation is the exception -- it must propagate.
        """
        async def one(entry):
            fn, args, kwargs = entry
            try:
                return await self.run(fn, *args, **kwargs)
            except Cancelled:
                raise
            except Exception as exc:
                self.facade.note(f"{getattr(fn, '__name__', fn)} failed: {exc}")
                return None

        return await asyncio.gather(*(one(c) for c in calls))
