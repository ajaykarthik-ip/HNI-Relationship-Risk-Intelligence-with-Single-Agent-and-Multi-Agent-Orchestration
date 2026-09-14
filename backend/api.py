#!/usr/bin/env python3
"""HTTP API over the screening and network pipelines.

    pip install -r requirements.txt
    uvicorn api:app --reload

    docs: http://127.0.0.1:8000/docs

A run takes 40-90 seconds, which is too long to hold an HTTP request open
through a browser or proxy. So work is submitted as a job and polled:

    POST /api/screening  {"name": "...", "company": "..."}   -> {"job_id": ...}
    GET  /api/jobs/{id}                                      -> status + progress
    GET  /api/jobs/{id}/result                               -> the report
    GET  /api/jobs/{id}/result.csv                           -> the CSV

Jobs live in memory. That is deliberate for an assessment deliverable and is
the first thing to replace for real use — see "Scaling" in ARCHITECTURE.md.
"""

from __future__ import annotations

import io
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from affluense import config, output as screening_output, usage
from affluense.cache import ResponseCache
from affluense.http import Fetcher
from affluense.network import output as network_output
from affluense.resolve import candidates
from affluense.sources import openai_client
from affluense.network.pipeline import Options as NetworkOptions
from affluense.network.pipeline import run as run_network
from affluense.pipeline import Options as ScreeningOptions
from affluense.pipeline import run as run_screening
from affluense_v2 import pipeline as v2_pipeline

config.load_env_file()

JobKind = Literal["screening", "network"]
JobStatus = Literal["queued", "running", "done", "error", "cancelled"]
# Which execution engine runs the job. "v1" is the stable sequential pipeline
# and stays the default, so every existing caller behaves exactly as before.
# "v2" is the concurrent orchestration engine in `affluense_v2`; both produce
# the same report through the same `output.build_report`.
Engine = Literal["v1", "v2"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class CandidateRequest(BaseModel):
    """The cheap first step. Free sources only, answered synchronously.

    This runs in a couple of seconds and spends no Firecrawl credits, so it
    needs none of the job machinery the research endpoints use.
    """

    name: str = Field(..., min_length=2, examples=["Virat Kohli"])
    company: str | None = Field(None, examples=["One8 Commune"])
    mode: Literal["person", "company"] = Field(
        "person", description="What the `name` field names."
    )
    use_cache: bool = True


class ConfirmedEntity(BaseModel):
    """A candidate the user picked, echoed back to start deep research."""

    id: str
    name: str
    kind: Literal["person", "company"] = "person"
    wikidata_id: str | None = None
    wikipedia_url: str | None = None
    description: str | None = None


class ScreeningRequest(BaseModel):
    name: str = Field(..., min_length=2, examples=["Mukesh Ambani"])
    company: str | None = Field(None, examples=["Reliance Industries"])
    max_companies: int = Field(12, ge=1, le=40)
    max_news: int = Field(25, ge=1, le=100)
    workers: int = Field(6, ge=1, le=16)
    use_firecrawl: bool = True
    use_cache: bool = True
    # Optional so the CLI and existing callers keep working. When supplied,
    # the pipeline trusts it instead of guessing the identity itself.
    confirmed: ConfirmedEntity | None = None
    engine: Engine = Field(
        "v1", description="v1 = stable sequential pipeline, v2 = concurrent."
    )


class NetworkRequest(BaseModel):
    name: str = Field(..., min_length=2, examples=["Azim Premji"])
    company: str | None = Field(None, examples=["Wipro"])
    max_suggestions: int = Field(25, ge=1, le=100)
    industries: int = Field(3, ge=1, le=6)
    roles: int = Field(3, ge=1, le=5)
    use_firecrawl: bool = True
    use_cache: bool = True
    confirmed: ConfirmedEntity | None = None
    engine: Engine = Field(
        "v1", description="v1 = stable sequential pipeline, v2 = concurrent."
    )


# ---------------------------------------------------------------------------
# Job store
# ---------------------------------------------------------------------------

class RunCancelled(Exception):
    """Raised inside the worker when the user asks a run to stop."""


# Phases in the order the pipeline runs them, with the percentage reached once
# each completes. Widths are roughly proportional to how long each takes.
#
# This is computed here, not in the browser, because only the server has the
# whole progress history: `summary()` sends the last 20 lines, and a run of
# 120 steps had long since pushed "merged to N entities" out of that window --
# so the client's own calculation fell back to its floor and sat at 2% for
# minutes while the run was half done.
_PHASES = (
    (re.compile(r"resolving identity", re.I), 6),
    (re.compile(r"wikidata claims|company links", re.I), 12),
    (re.compile(r"co-officer", re.I), 16),
    (re.compile(r"firecrawl: search", re.I), 20),
    (re.compile(r"^\s*(search|read):", re.I), 46),
    (re.compile(r"company records merged", re.I), 52),
)

_MERGED = re.compile(r"merged to (\d+) entit", re.I)
_SCREENING_COMPANY = re.compile(r"^\s*screening:\s", re.I)
_SCREENING_PERSON = re.compile(r"screening the individual", re.I)

_SCREEN_FROM, _SCREEN_TO = 52, 90


def run_progress(progress: list, status: JobStatus, engine: Engine = "v1",
                 state: dict | None = None) -> tuple:
    """(percent, phase) for a run, from its full step history.

    Never reports 100 while work is outstanding: a bar sitting full while the
    user waits is worse than one sitting at 97.

    V2 does not go through the string matching below, and could not: with nine
    evidence agents in flight the "Screening: X" lines arrive interleaved, so
    counting them says nothing about how much work is left. It reports a
    structured event instead and this reads it verbatim. V1's path is
    unchanged, character for character.
    """
    if status in ("done", "error", "cancelled"):
        return 100, status
    if engine == "v2":
        if not state:
            return 2, "starting"
        return int(state.get("percent", 2)), state.get("phase", "running")
    if not progress:
        return 2, "starting"

    expected = 0
    screened = 0
    person = False
    for line in progress:
        match = _MERGED.search(line)
        if match:
            expected = int(match.group(1))
        if _SCREENING_COMPANY.search(line):
            screened += 1
        if _SCREENING_PERSON.search(line):
            person = True

    if person:
        return 95, "screening the individual"

    if expected and screened:
        share = min(screened / expected, 1.0)
        percent = _SCREEN_FROM + (_SCREEN_TO - _SCREEN_FROM) * share
        return int(min(percent, 90)), f"screening company {screened} of {expected}"

    percent, phase = 2, "starting"
    for pattern, reached in _PHASES:
        if any(pattern.search(line) for line in progress):
            percent = max(percent, reached)
            phase = "gathering evidence" if reached >= 20 else "resolving identity"
    return percent, phase


@dataclass
class Job:
    id: str
    kind: JobKind
    name: str
    company: str | None
    # Which engine ran it. Stored on the job so the UI can label a finished
    # report and a benchmark can tell two runs of the same subject apart.
    engine: Engine = "v1"
    status: JobStatus = "queued"
    progress: list = field(default_factory=list)
    result: dict | None = None
    error: str | None = None
    created_at: str = field(default_factory=utc_now)
    finished_at: str | None = None
    # Set by /cancel. The worker checks it every time the pipeline reports a
    # step, which is often enough to stop within a second or two.
    cancelled: threading.Event = field(default_factory=threading.Event)
    # V2 only: the last structured progress event. V1 leaves this None and its
    # percentage keeps coming from the step history.
    v2_state: dict | None = None

    def summary(self) -> dict:
        percent, phase = run_progress(
            self.progress, self.status, self.engine, self.v2_state,
        )
        stages = (self.v2_state or {}).get("stages") if self.engine == "v2" else None
        return {
            "job_id": self.id,
            "kind": self.kind,
            "engine": self.engine,
            "name": self.name,
            "company": self.company,
            "status": self.status,
            "progress": self.progress[-20:],
            "steps_completed": len(self.progress),
            "percent": percent,
            "phase": phase,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "result_available": self.result is not None,
            "stages": stages,
        }


class JobStore:
    """In-memory, thread-safe. Replace with a queue and a database for real use."""

    def __init__(self, max_workers: int = 2):
        self._jobs: dict = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=max_workers)

    def create(self, kind: JobKind, name: str, company: str | None,
               engine: Engine = "v1") -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, name=name,
                  company=company, engine=engine)
        with self._lock:
            self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def all(self) -> list:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def delete(self, job_id: str) -> bool:
        with self._lock:
            return self._jobs.pop(job_id, None) is not None

    def cancel(self, job_id: str) -> bool:
        """Ask a running job to stop. True if there was one to stop."""
        job = self.get(job_id)
        if job is None or job.status not in ("queued", "running"):
            return False
        job.cancelled.set()
        return True

    def submit(self, job: Job, work) -> None:
        def runner():
            job.status = "running"

            def on_progress(line: str) -> None:
                """The pipeline's step reporter, and the cancellation point.

                A run makes hundreds of requests and can take minutes, so
                stopping it has to be cooperative -- there is no safe way to
                kill a thread mid-request. Every step the pipeline reports is
                a chance to unwind, which in practice means a stop takes
                effect within a second or two and never mid-write.
                """
                if job.cancelled.is_set():
                    raise RunCancelled()
                job.progress.append(line)

            def on_event(event: dict) -> None:
                """V2's structured progress, and a second cancellation point.

                V2 reports stage state that no amount of regexing the log could
                reconstruct once several agents are running at once. It is
                stored verbatim and read back by `run_progress`.
                """
                if job.cancelled.is_set():
                    raise RunCancelled()
                job.v2_state = event

            try:
                job.result = work(on_progress, on_event)
                job.status = "done"
            except RunCancelled:
                job.status = "cancelled"
                job.progress.append("Stopped at your request.")
            except Exception as exc:  # a failed job must not kill the worker
                job.error = f"{type(exc).__name__}: {exc}"
                job.status = "error"
            finally:
                job.finished_at = utc_now()

        self._pool.submit(runner)


store = JobStore()

app = FastAPI(
    title="Affluense",
    version="2.0.0",
    description=(
        "Screening and network intelligence for high-net-worth individuals. "
        "Runs are asynchronous: submit a job, then poll it."
    ),
)

# The Next.js dev server is the intended consumer. A fixed port list is too
# brittle for local work: Next moves to 3001 whenever 3000 is taken, and the
# only symptom is "OPTIONS /health 400" with an unreachable-backend banner.
# Any loopback port is allowed instead; set ALLOWED_ORIGINS to pin it down.
LOCALHOST_ORIGIN = r"https?://(localhost|127\.0\.0\.1)(:\d+)?"

_configured = os.environ.get("ALLOWED_ORIGINS")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_configured.split(",") if _configured else [],
    allow_origin_regex=None if _configured else LOCALHOST_ORIGIN,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health", tags=["meta"])
def health() -> dict:
    """Readiness, and which optional capabilities are actually available."""
    from affluense.enrich.sentiment import SentimentAnalyzer

    backend = SentimentAnalyzer().backend
    return {
        "status": "ok",
        "sentiment_backend": backend,
        "sentiment_degraded": "DEGRADED" in backend,
        "firecrawl_configured": bool(os.environ.get("FIRECRAWL_API_KEY")),
        "openai": openai_client.describe_configuration(),
        "jobs": len(store.all()),
        # The UI hides the engine selector when only one engine is available.
        "engines": ["v1", "v2"],
        "default_engine": "v1",
        "v2": v2_pipeline.describe(),
    }


@app.post("/api/candidates", tags=["identity"])
def find_candidates(request: CandidateRequest) -> dict:
    """Who did you mean? Free sources only — no Firecrawl, no credits spent.

    Synchronous by design: it answers in seconds, and the user is waiting on
    it to choose. Deep research starts only after they pick a row and one of
    the endpoints below is called with `confirmed`.
    """
    fetcher = Fetcher(
        cache=ResponseCache(".cache" if request.use_cache else None,
                            enabled=request.use_cache),
    )
    rate, rate_source = usage.resolve_inr_rate(fetcher)
    fetcher.meter.usd_to_inr = rate
    fetcher.meter.rate_source = rate_source

    return candidates.discover(
        fetcher, request.name, request.company, mode=request.mode
    )


@app.post("/api/screening", status_code=202, tags=["screening"])
def start_screening(request: ScreeningRequest) -> dict:
    """Problem Statement 1: connected companies, sentiment, adverse-news flags.

    `engine` picks which pipeline runs it. Both hand the same result dict to the
    same `output.build_report`, so the delivered report has an identical shape
    and the two can be compared on one subject.
    """
    job = store.create("screening", request.name, request.company,
                       request.engine)

    def work(on_progress, on_event):
        firecrawl_key = (os.environ.get("FIRECRAWL_API_KEY")
                         if request.use_firecrawl else None)
        cache_dir = ".cache" if request.use_cache else None
        confirmed = (request.confirmed.model_dump()
                     if request.confirmed else None)

        if request.engine == "v2":
            options = v2_pipeline.Options(
                max_companies=request.max_companies,
                max_news=request.max_news,
                workers=request.workers,
                firecrawl_key=firecrawl_key,
                cache_dir=cache_dir,
                verbose=False,
                on_progress=on_progress,
                on_event=on_event,
                confirmed_entity=confirmed,
            )
            result = v2_pipeline.run(request.name, request.company, options)
            # V2 assembles through V1's builder, then applies its own
            # confidence cap. Same report shape either way.
            return v2_pipeline.build_report(result, request.name, request.company)
        else:
            options = ScreeningOptions(
                max_companies=request.max_companies,
                max_news=request.max_news,
                workers=request.workers,
                firecrawl_key=firecrawl_key,
                cache_dir=cache_dir,
                verbose=False,
                on_progress=on_progress,
                confirmed_entity=confirmed,
            )
            result = run_screening(request.name, request.company, options)

        return screening_output.build_report(result, request.name, request.company)

    store.submit(job, work)
    return job.summary()


@app.post("/api/network", status_code=202, tags=["network"])
def start_network(request: NetworkRequest) -> dict:
    """Problem Statement 2: current network and ranked connection suggestions."""
    job = store.create("network", request.name, request.company,
                       request.engine)

    def work(on_progress, on_event):
        firecrawl_key = (os.environ.get("FIRECRAWL_API_KEY")
                         if request.use_firecrawl else None)
        cache_dir = ".cache" if request.use_cache else None
        confirmed = (request.confirmed.model_dump()
                     if request.confirmed else None)

        if request.engine == "v2":
            options = v2_pipeline.Options(
                max_suggestions=request.max_suggestions,
                industries=request.industries,
                roles=request.roles,
                firecrawl_key=firecrawl_key,
                cache_dir=cache_dir,
                verbose=False,
                on_progress=on_progress,
                on_event=on_event,
                confirmed_entity=confirmed,
            )
            result = v2_pipeline.run_network(request.name, request.company,
                                             options)
        else:
            options = NetworkOptions(
                max_suggestions=request.max_suggestions,
                industries=request.industries,
                roles=request.roles,
                firecrawl_key=firecrawl_key,
                cache_dir=cache_dir,
                verbose=False,
                on_progress=on_progress,
                confirmed_entity=confirmed,
            )
            result = run_network(request.name, request.company, options)

        return network_output.build_report(result, request.name, request.company)

    store.submit(job, work)
    return job.summary()


@app.get("/api/jobs", tags=["jobs"])
def list_jobs() -> dict:
    return {"jobs": [job.summary() for job in store.all()]}


def _require(job_id: str) -> Job:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, f"No job {job_id}")
    return job


@app.get("/api/jobs/{job_id}", tags=["jobs"])
def job_status(job_id: str) -> dict:
    return _require(job_id).summary()


@app.post("/api/jobs/{job_id}/cancel", tags=["jobs"])
def cancel_job(job_id: str) -> dict:
    """Stop a running job.

    Closing the browser is not enough on its own: the worker keeps going and
    keeps spending Firecrawl credits and OpenAI tokens on a report nobody is
    waiting for. This is what actually stops the work.
    """
    job = _require(job_id)
    stopped = store.cancel(job_id)
    return {
        "job_id": job_id,
        "stopping": stopped,
        "status": job.status,
        "detail": (
            "The run will stop at its next step, usually within a second or two."
            if stopped
            else f"Job is already {job.status}; nothing to stop."
        ),
    }


@app.get("/api/jobs/{job_id}/result", tags=["jobs"])
def job_result(job_id: str) -> dict:
    job = _require(job_id)
    if job.status == "error":
        raise HTTPException(500, job.error or "The job failed")
    if job.status != "done":
        # 409, not 404: the job exists, it just is not finished.
        raise HTTPException(409, f"Job {job_id} is {job.status}; poll /api/jobs/{job_id}")
    return job.result


@app.get("/api/jobs/{job_id}/result.csv", tags=["jobs"])
def job_result_csv(job_id: str):
    job = _require(job_id)
    if job.status != "done":
        raise HTTPException(409, f"Job {job_id} is {job.status}")

    buffer = io.StringIO()
    writer = (screening_output if job.kind == "screening" else network_output)
    # The writers take a path; render to a temporary file, then stream it.
    import tempfile

    with tempfile.NamedTemporaryFile("w+", suffix=".csv", delete=False,
                                     encoding="utf-8", newline="") as handle:
        path = handle.name
    writer.write_csv(job.result, path)
    with open(path, encoding="utf-8") as handle:
        buffer.write(handle.read())
    os.unlink(path)
    buffer.seek(0)

    filename = f"{job.name.lower().replace(' ', '-')}-{job.kind}.csv"
    return StreamingResponse(
        buffer,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/api/jobs/{job_id}", tags=["jobs"])
def delete_job(job_id: str) -> dict:
    if not store.delete(job_id):
        raise HTTPException(404, f"No job {job_id}")
    return {"deleted": job_id}
