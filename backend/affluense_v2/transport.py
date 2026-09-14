"""The one async transport: bounded per host, retrying, cached, metered.

The V2 counterpart of `affluense/http.py`, and deliberately a separate file
rather than an async mode bolted onto `Fetcher`. Sharing that class would mean
editing V1, which is the one thing this engine must not do.

What carries over unchanged: the cache keys, the TTLs, the retry statuses, the
usage meter, and the rule that a failed request is a note rather than an
exception. What changes is the gate. V1 sleeps one second between requests to
the same host; V2 holds a semaphore and a token bucket sized per host, which is
what turns a 2-request-per-second ceiling into real concurrency without
pointing more load at any single publisher.

One extra job lives here: the run-scoped OpenAI ceiling. Enforcing it in the
transport rather than in a client means it binds every call site, including V1
modules reached through the bridge that build their own clients and know
nothing about V2.
"""

from __future__ import annotations

import asyncio
import json as _json
import random
import urllib.parse
import urllib.robotparser

import httpx

from affluense import config as v1config
from affluense.usage import UsageMeter

from . import config as v2config
from .cache_async import AsyncCache
from .concurrency import gather_ordered
from . import hostpolicy
from .hostpolicy import GateRegistry
from .progress import Cancelled


def _timeout_kwarg(timeout: float | None) -> dict:
    """Pass a timeout only when one was asked for.

    httpx reads `timeout=None` as "no timeout at all", not as "use the client
    default" -- so forwarding a None straight through would quietly remove the
    ceiling from every ordinary request. Omitting the argument is what keeps
    the client's configured default.
    """
    return {} if timeout is None else {"timeout": timeout}


class AsyncResponse:
    """Enough of `requests.Response` for V1's parsers to work unchanged.

    `news.py` reads `.content` to hand bytes to ElementTree; `fulltext.py`
    reads `.text`. Both are served from one object whether the body came off
    the wire or off disk.
    """

    __slots__ = ("text", "url", "status_code", "_cached")

    def __init__(self, text: str, url: str, status_code: int = 200,
                 cached: bool = False):
        self.text = text
        self.url = url
        self.status_code = status_code
        self._cached = cached

    @property
    def content(self) -> bytes:
        return self.text.encode("utf-8")

    @property
    def from_cache(self) -> bool:
        return self._cached

    def json(self):
        return _json.loads(self.text)


class AsyncFetcher:
    """Shared transport for one V2 run.

    Created inside the running loop, because the host gates hold asyncio
    primitives. One client is reused for the whole run so connections -- and
    with HTTP/2, streams -- are not rebuilt per request.
    """

    def __init__(self, cache: AsyncCache, meter: UsageMeter, reporter,
                 budgets, client: httpx.AsyncClient):
        self.cache = cache
        self.meter = meter
        self.reporter = reporter
        self.budgets = budgets
        self.client = client
        self.gates = GateRegistry()
        self.notes: list = []
        self._notes_lock = asyncio.Lock()
        self._robots: dict = {}
        self._robots_locks: dict = {}

    # -- construction -------------------------------------------------------
    @classmethod
    def build(cls, cache, meter, reporter, budgets) -> "AsyncFetcher":
        timeout = httpx.Timeout(
            v2config.READ_TIMEOUT,
            connect=v2config.CONNECT_TIMEOUT,
        )
        limits = httpx.Limits(
            max_connections=v2config.POOL_LIMIT,
            max_keepalive_connections=v2config.POOL_LIMIT,
        )
        try:
            client = httpx.AsyncClient(
                http2=v2config.HTTP2,
                timeout=timeout,
                limits=limits,
                follow_redirects=True,
                headers={
                    "User-Agent": v2config.USER_AGENT,
                    "Accept-Language": "en",
                },
            )
        except ImportError:
            # http2 needs the `h2` extra. Falling back is strictly better than
            # failing the run over a transport optimisation.
            client = httpx.AsyncClient(
                http2=False,
                timeout=timeout,
                limits=limits,
                follow_redirects=True,
                headers={
                    "User-Agent": v2config.USER_AGENT,
                    "Accept-Language": "en",
                },
            )
        return cls(cache, meter, reporter, budgets, client)

    async def aclose(self) -> None:
        await self.client.aclose()

    # -- notes --------------------------------------------------------------
    def note(self, message: str) -> None:
        """Deduplicated, and safe to call from a worker thread.

        Uses a plain membership test rather than the async lock: V1 source
        modules call this from inside `to_thread`, where awaiting is not an
        option. A duplicated note under a race is harmless; a deadlock is not.
        """
        if message not in self.notes:
            self.notes.append(message)

    # -- core ---------------------------------------------------------------
    def _backoff(self, attempt: int) -> float:
        delay = min(v2config.BACKOFF_BASE * (2 ** (attempt - 1)),
                    v2config.BACKOFF_CAP)
        # Jitter, so a burst that was rate-limited together does not retry
        # together and get rate-limited again.
        return delay * (0.5 + random.random() * 0.5)

    async def get(self, url: str, attempts: int | None = None, params=None,
                  headers=None, timeout: float | None = None):
        """GET with backoff, served from cache when fresh. None on failure."""
        self.reporter.check()
        attempts = attempts or v2config.MAX_ATTEMPTS

        cache_key = self.cache.key("GET", url, params)
        cached = await self.cache.get(cache_key, url)
        if cached is not None:
            self.meter.record(url, cached=True)
            return AsyncResponse(cached, url, cached=True)

        gate = self.gates.gate(url)
        for attempt in range(1, attempts + 1):
            self.reporter.check()
            async with gate:
                try:
                    response = await self.client.get(
                        url, params=params, headers=headers,
                        **_timeout_kwarg(timeout),
                    )
                except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                    if attempt == attempts:
                        self.note(f"Request failed for {url}: {exc}")
                        return None
                    await asyncio.sleep(self._backoff(attempt))
                    continue

            self.meter.record(url)
            if response.status_code == 200:
                text = response.text
                await self.cache.set(cache_key, text)
                return AsyncResponse(text, str(response.url))

            if response.status_code == 429 and gate.penalise():
                self.note(
                    f"{gate.host} asked us to slow down (HTTP 429); its rate "
                    "was halved for the rest of this run."
                )
            if response.status_code in v2config.RETRY_STATUS and attempt < attempts:
                await asyncio.sleep(self._backoff(attempt))
                continue

            self._note_status(response.status_code, url)
            return None
        return None

    def _note_status(self, status: int, url: str) -> None:
        """Record a refusal, and say what to do about it where we know.

        A 403 from Wikimedia is almost always the User-Agent policy rather than
        an outage, and it silently removes registry discovery and the whole of
        PS2. Reading "HTTP 403" and moving on is how that goes unnoticed.
        """
        host = hostpolicy.host_of(url)
        if status == 403 and any(
            fragment in host for fragment in v2config.CONTACT_SENSITIVE_HOSTS
        ):
            if self.budgets.note_once("wikimedia-403"):
                self.note(
                    f"{host} refused the request (HTTP 403). This is usually "
                    "the User-Agent policy: set AFFLUENSE_CONTACT in "
                    "backend/.env to an address or URL you monitor. Without "
                    "it, registry company links, role dates and the "
                    "structured network are all unavailable."
                )
            return
        self.note(f"HTTP {status} from {url}")

    async def get_json(self, url: str, **kwargs):
        response = await self.get(url, **kwargs)
        if response is None:
            return None
        try:
            return response.json()
        except ValueError:
            self.note(f"Response from {url} was not JSON")
            return None

    async def post_json(self, url: str, payload: dict, headers: dict,
                        attempts: int | None = None, kind: str | None = None,
                        units: int = 1, on_response=None,
                        timeout: float | None = None):
        """POST, cached. Paid calls must never be paid for twice.

        `kind` and `units` say what this call bills. `on_response(parsed, cached)`
        exists for services billed by something inside the response -- OpenAI
        returns the token counts it charged for -- and is told whether the answer
        came off disk so a cache hit is never billed again.
        """
        self.reporter.check()
        attempts = attempts or v2config.MAX_ATTEMPTS

        cache_key = self.cache.key("POST", url, None, payload)
        cached = await self.cache.get(cache_key, url)
        if cached is not None:
            self.meter.record(url, kind, units, cached=True)
            parsed = _json.loads(cached)
            if on_response:
                on_response(parsed, True)
            return parsed

        # The run-scoped OpenAI ceiling. Enforced here so it also binds V1
        # clients reached through the bridge, which know nothing about V2.
        # A refusal is not an error: the caller falls back to its deterministic
        # path exactly as it does when no key is configured.
        if url == v1config.OPENAI_CHAT:
            if not self.budgets.openai_calls.claim(1):
                if self.budgets.note_once("openai-budget"):
                    self.note(
                        "The run's OpenAI call budget was reached; the "
                        "remaining evidence keeps its deterministic "
                        "annotation."
                    )
                return None

        gate = self.gates.gate(url)
        for attempt in range(1, attempts + 1):
            self.reporter.check()
            async with gate:
                try:
                    response = await self.client.post(
                        url, json=payload, headers=headers,
                        **_timeout_kwarg(timeout),
                    )
                except (httpx.HTTPError, asyncio.TimeoutError) as exc:
                    if attempt == attempts:
                        self.note(f"Request failed for {url}: {exc}")
                        return None
                    await asyncio.sleep(self._backoff(attempt))
                    continue

            # Recorded on dispatch: a 500 from a paid endpoint can still have
            # consumed a credit, and under-reporting cost is the worse error.
            self.meter.record(url, kind, units)
            if response.status_code == 200:
                try:
                    parsed = response.json()
                except ValueError:
                    self.note(f"Response from {url} was not JSON")
                    return None
                await self.cache.set(cache_key, response.text)
                if on_response:
                    on_response(parsed, False)
                return parsed

            if response.status_code == 429 and gate.penalise():
                self.note(
                    f"{gate.host} asked us to slow down (HTTP 429); its rate "
                    "was halved for the rest of this run."
                )
            if response.status_code in v2config.RETRY_STATUS and attempt < attempts:
                await asyncio.sleep(self._backoff(attempt))
                continue

            # Never echo the body: it can repeat an API key back.
            self._note_status(response.status_code, url)
            return None
        return None

    # -- politeness ---------------------------------------------------------
    async def robots_allow(self, url: str) -> bool:
        """Ask the site whether this client may read the page.

        V1 reads robots.txt through `urllib.robotparser`, which uses its own
        opener with no timeout, no pooling, no cache and no metering. One
        unresponsive publisher stalls the entire run there. Here it is an
        ordinary request: timed, pooled, gated and cached like any other.

        Semantics are V1's, deliberately. A reachable file is obeyed; an
        unreachable one means the site is skipped rather than guessed at. The
        one refinement is that a 4xx -- overwhelmingly a site with no
        robots.txt at all -- is treated as permission, which is what
        `robotparser` itself does.
        """
        parts = urllib.parse.urlsplit(url)
        root = f"{parts.scheme}://{parts.netloc}"

        parser = self._robots.get(root)
        if parser is None:
            lock = self._robots_locks.setdefault(root, asyncio.Lock())
            async with lock:
                parser = self._robots.get(root)
                if parser is None:
                    parser = await self._load_robots(root)
                    self._robots[root] = parser

        if parser is False:
            return False
        if parser is True:
            return True
        try:
            return parser.can_fetch(v1config.USER_AGENT, url)
        except Exception:
            return False

    async def _load_robots(self, root: str):
        """True = no rules (allow), False = skip the site, else a parser."""
        self.reporter.check()
        gate = self.gates.gate(root)
        try:
            async with gate:
                response = await self.client.get(
                    f"{root}/robots.txt", timeout=v2config.ROBOTS_TIMEOUT,
                )
        except (httpx.HTTPError, asyncio.TimeoutError):
            self.note(f"Could not read robots.txt for {root}; skipping its pages")
            return False

        if response.status_code >= 400:
            # No robots.txt is permission, which is how robotparser reads it.
            return True

        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(f"{root}/robots.txt")
        try:
            parser.parse(response.text.splitlines())
        except Exception:
            self.note(f"robots.txt for {root} did not parse; skipping its pages")
            return False
        return parser

    async def prefetch_robots(self, urls: list) -> None:
        """Warm every distinct origin at once, before the fetch loop.

        Serially this is the single slowest thing V1 does per article. Done up
        front and concurrently it is close to free, and it means the fetch loop
        below never blocks on a robots lookup.
        """
        roots = []
        seen = set()
        for url in urls:
            parts = urllib.parse.urlsplit(url or "")
            if not parts.netloc:
                continue
            root = f"{parts.scheme}://{parts.netloc}"
            if root not in seen and root not in self._robots:
                seen.add(root)
                roots.append(root)
        if not roots:
            return

        async def one(root: str) -> None:
            try:
                lock = self._robots_locks.setdefault(root, asyncio.Lock())
                async with lock:
                    if root not in self._robots:
                        self._robots[root] = await self._load_robots(root)
            except Cancelled:
                raise
            except Exception:
                # An origin we cannot ask is an origin we do not read, which is
                # V1's rule too.
                self._robots[root] = False

        # gather_ordered rather than asyncio.gather: a cancelled run has to
        # stop here, and return_exceptions=True would swallow that.
        await gather_ordered([one(r) for r in roots])

    # -- reporting ----------------------------------------------------------
    def describe(self) -> dict:
        return {"hosts": self.gates.describe()}
