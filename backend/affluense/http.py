"""One HTTP client for every source: polite, retrying, robots-aware."""

from __future__ import annotations

import threading
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field

import requests

from . import config
from .cache import ResponseCache
from .usage import UsageMeter


class CachedResponse:
    """Stand-in for requests.Response, served from disk."""

    __slots__ = ("text", "url", "status_code")

    def __init__(self, text: str, url: str):
        self.text = text
        self.url = url
        self.status_code = 200

    @property
    def content(self) -> bytes:
        return self.text.encode("utf-8")

    def json(self):
        import json as _json
        return _json.loads(self.text)


@dataclass
class Fetcher:
    """Shared transport.

    Sources never build their own session. Keeping rate limiting, retries and
    robots.txt in one place means a politeness fix applies everywhere at once.
    """

    delay: float = config.DEFAULT_DELAY
    timeout: int = config.DEFAULT_TIMEOUT
    session: requests.Session = field(default_factory=requests.Session)
    notes: list = field(default_factory=list)
    cache: ResponseCache = field(default_factory=ResponseCache)
    meter: UsageMeter = field(default_factory=UsageMeter)
    _robots: dict = field(default_factory=dict)
    _host_last_call: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.session.headers.update(
            {"User-Agent": config.USER_AGENT, "Accept-Language": "en"}
        )
        # One pooled connection per worker, so threads do not contend.
        adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # -- core ---------------------------------------------------------------
    def _wait(self, url: str) -> None:
        """Rate limit per host, not globally.

        A global limiter would serialise every worker even when they are
        talking to unrelated services. Politeness is owed to each host
        separately.
        """
        host = urllib.parse.urlsplit(url).netloc
        while True:
            with self._lock:
                now = time.monotonic()
                last = self._host_last_call.get(host, 0.0)
                wait = self.delay - (now - last)
                if wait <= 0:
                    self._host_last_call[host] = now
                    return
            time.sleep(wait)

    def get(self, url: str, attempts: int = 3, **kwargs):
        """GET with backoff, served from cache when fresh.

        Several sources throttle politely rather than failing, so one 429
        must not drop a whole source.
        """
        timeout = kwargs.pop("timeout", self.timeout)

        cache_key = self.cache.key("GET", url, kwargs.get("params"))
        cached = self.cache.get(cache_key, url)
        if cached is not None:
            self.meter.record(url, cached=True)
            return CachedResponse(cached, url)

        for attempt in range(1, attempts + 1):
            self._wait(url)
            try:
                response = self.session.get(url, timeout=timeout, **kwargs)
            except requests.RequestException as exc:
                if attempt == attempts:
                    self.note(f"Request failed for {url}: {exc}")
                    return None
                time.sleep(self.delay * 2 * attempt)
                continue

            self.meter.record(url)
            if response.status_code == 200:
                self.cache.set(cache_key, response.text)
                return response
            if response.status_code in config.RETRY_STATUS and attempt < attempts:
                time.sleep(self.delay * 3 * attempt)
                continue
            self.note(f"HTTP {response.status_code} from {url}")
            return None
        return None

    def get_json(self, url: str, **kwargs):
        response = self.get(url, **kwargs)
        if response is None:
            return None
        try:
            return response.json()
        except ValueError:
            self.note(f"Response from {url} was not JSON")
            return None

    def post_json(self, url: str, payload: dict, headers: dict, attempts: int = 3,
                  kind: str | None = None, units: int = 1,
                  on_response=None, timeout: int | None = None):
        """POST, cached. Firecrawl calls cost credits, so a repeated request
        must never be paid for twice.

        `kind` and `units` say what this call bills, so the meter counts
        credits where they are actually spent rather than guessing later.

        `on_response(parsed, cached)` is for services billed by something
        inside the response rather than by the call itself. OpenAI returns the
        token counts it charged for, and the callback is told whether the
        answer came off disk so a cache hit is never billed twice.
        """
        cache_key = self.cache.key("POST", url, None, payload)
        cached = self.cache.get(cache_key, url)
        if cached is not None:
            self.meter.record(url, kind, units, cached=True)
            import json as _json
            parsed = _json.loads(cached)
            if on_response:
                on_response(parsed, True)
            return parsed

        for attempt in range(1, attempts + 1):
            self._wait(url)
            try:
                response = self.session.post(
                    url, json=payload, headers=headers,
                    timeout=timeout or self.timeout,
                )
            except requests.RequestException as exc:
                if attempt == attempts:
                    self.note(f"Request failed for {url}: {exc}")
                    return None
                time.sleep(self.delay * 2 * attempt)
                continue

            # Recorded on dispatch: a 500 from a paid endpoint can still
            # have consumed a credit, and under-reporting cost is the worse
            # error of the two.
            self.meter.record(url, kind, units)
            if response.status_code == 200:
                try:
                    parsed = response.json()
                except ValueError:
                    self.note(f"Response from {url} was not JSON")
                    return None
                self.cache.set(cache_key, response.text)
                if on_response:
                    on_response(parsed, False)
                return parsed
            if response.status_code in config.RETRY_STATUS and attempt < attempts:
                time.sleep(self.delay * 3 * attempt)
                continue
            # Never echo the body: it can repeat an API key back.
            self.note(f"HTTP {response.status_code} from {url}")
            return None
        return None

    # -- politeness ---------------------------------------------------------
    def robots_allow(self, url: str) -> bool:
        """Ask the site whether this client may read the page."""
        parts = urllib.parse.urlsplit(url)
        root = f"{parts.scheme}://{parts.netloc}"
        parser = self._robots.get(root)

        if parser is None:
            parser = urllib.robotparser.RobotFileParser()
            parser.set_url(f"{root}/robots.txt")
            try:
                parser.read()
            except Exception:
                # No reachable robots.txt: stay conservative and skip the site.
                self.note(f"Could not read robots.txt for {root}; skipping its pages")
                self._robots[root] = False
                return False
            self._robots[root] = parser

        if parser is False:
            return False
        return parser.can_fetch(config.USER_AGENT, url)

    def note(self, message: str) -> None:
        with self._lock:
            if message not in self.notes:
                self.notes.append(message)
