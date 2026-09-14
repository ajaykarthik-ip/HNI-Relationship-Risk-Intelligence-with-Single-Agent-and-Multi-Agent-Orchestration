"""Fan-out helpers with the two properties this pipeline needs.

**Order is preserved.** Results come back in the order the work was submitted,
never the order it finished. Findings, screenings and evidence lists are
assembled from these results, so letting the network decide the order would make
two runs over identical evidence produce different reports. This is the single
rule that keeps V2 as reproducible as V1.

**Failure is contained, cancellation is not.** One dead source must not sink a
run -- it becomes a note and a `None` in place. But a cancelled run must stop,
so `Cancelled` is always re-raised. `asyncio.gather(return_exceptions=True)`
alone gets this wrong: it swallows cancellation along with everything else,
which is how a stopped job carries on spending credits.
"""

from __future__ import annotations

import asyncio

from .progress import Cancelled


def _first_cancellation(results: list):
    for item in results:
        if isinstance(item, (Cancelled, asyncio.CancelledError)):
            return item
    return None


async def gather_ordered(coros: list, on_error=None) -> list:
    """Run everything concurrently; return results in submission order.

    Failures become `None` (after `on_error(index, exc)` is called, if given).
    Cancellation propagates.
    """
    coros = list(coros)
    if not coros:
        return []

    results = await asyncio.gather(*coros, return_exceptions=True)

    cancelled = _first_cancellation(results)
    if cancelled is not None:
        raise cancelled

    output = []
    for index, item in enumerate(results):
        if isinstance(item, BaseException):
            if on_error is not None:
                on_error(index, item)
            output.append(None)
        else:
            output.append(item)
    return output


async def map_bounded(items: list, worker, limit: int, on_error=None) -> list:
    """`worker(item)` over every item, at most `limit` at once, in order.

    The bound is a second line of defence. The host gates already cap real
    network load; this stops the task list itself from growing without bound
    when a subject turns out to have forty connected companies.
    """
    items = list(items)
    if not items:
        return []
    semaphore = asyncio.Semaphore(max(1, limit))

    async def guarded(item):
        async with semaphore:
            return await worker(item)

    return await gather_ordered([guarded(i) for i in items], on_error=on_error)
