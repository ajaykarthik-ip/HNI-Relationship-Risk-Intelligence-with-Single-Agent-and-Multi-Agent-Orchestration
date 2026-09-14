"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelJob,
  csvUrl,
  findCandidates,
  getHealth,
  runJob,
  type Candidate,
  type Engine,
  type CandidateResponse,
  type Health,
  type Job,
  type JobKind,
  type NetworkReport,
  type ScreeningReport,
} from "@/lib/api";
import { CandidatePicker } from "@/components/candidate-picker";
import { CostPanel } from "@/components/cost-panel";
import { EngineCompare, type EngineRun } from "@/components/engine-compare";
import { EngineSelector } from "@/components/engine-selector";
import { NetworkView } from "@/components/v2/network-view";
import { RunStatus } from "@/components/v2/run-status";
import { ScreeningReportView } from "@/components/v2/screening-report";

type Mode = JobKind | "both";

// Hidden for now. `useCache` still defaults to false, so every run is fresh --
// which is what you want while the engines are being compared, because a warm
// cache makes whichever engine runs second look faster than it is.
const SHOW_CACHE_TOGGLE = false;

const MODES: { id: Mode; label: string; blurb: string }[] = [
  {
    id: "screening",
    label: "Risk screening",
    blurb: "Their other companies, the news on each, and anything adverse.",
  },
  {
    id: "network",
    label: "Network",
    blurb: "Who they already know, and who they should meet next.",
  },
  {
    id: "both",
    label: "Both",
    blurb: "Screening first, then the network — the second run reuses the first's cache.",
  },
];

export default function Home() {
  const [health, setHealth] = useState<Health | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);

  const [name, setName] = useState("");
  const [company, setCompany] = useState("");
  const [mode, setMode] = useState<Mode>("screening");
  // Which backend engine runs the research. V1 stays the default: it is the
  // stable pipeline, and the selector exists to compare against it rather than
  // to quietly replace it.
  const [engine, setEngine] = useState<Engine>("v1");
  const [useFirecrawl, setUseFirecrawl] = useState(true);
  // Off while the pipeline is being worked on: news changes between runs and a
  // cached answer hides whether a fix actually did anything. Turn it back on
  // once the results are trusted — a cached run spends no Firecrawl credits.
  const [useCache, setUseCache] = useState(false);

  // What the typed name refers to. "company" lets the user start from a
  // company and pick a person out of it.
  const [lookingFor, setLookingFor] = useState<"person" | "company">("person");

  const [runs, setRuns] = useState<{
    screening?: { job: Job; report: ScreeningReport };
    network?: { job: Job; report: NetworkReport };
  }>({});
  // Every screening this subject has produced, keyed by engine, so a V1 result
  // and a V2 result sit side by side instead of overwriting each other.
  // Cleared whenever the subject changes.
  const [compare, setCompare] = useState<Partial<Record<Engine, EngineRun>>>({});
  const [liveJob, setLiveJob] = useState<Job | null>(null);
  const [view, setView] = useState<JobKind>("screening");
  const [error, setError] = useState<string | null>(null);
  const [elapsed, setElapsed] = useState(0);
  const abort = useRef<AbortController | null>(null);

  // Identity first, research second. Nothing expensive runs until a candidate
  // is picked, which is the whole point of the two-step flow.
  const [candidates, setCandidates] = useState<CandidateResponse | null>(null);
  const [findingCandidates, setFindingCandidates] = useState(false);
  const [stopping, setStopping] = useState(false);

  const running = liveJob?.status === "queued" || liveJob?.status === "running";
  const both = Boolean(runs.screening && runs.network);
  const shown = view === "screening" ? runs.screening : runs.network;

  useEffect(() => {
    getHealth()
      .then(setHealth)
      .catch((e) => setHealthError(e.message));
  }, []);

  useEffect(() => {
    if (!running) return;
    const started = Date.now();
    const timer = setInterval(() => setElapsed(Math.round((Date.now() - started) / 1000)), 1000);
    return () => clearInterval(timer);
  }, [running]);

  /** Step one. Free sources only, so this is safe to run on every search. */
  const findWhoTheyMean = useCallback(async () => {
    setError(null);
    setRuns({});
    setCompare({});
    setLiveJob(null);
    setCandidates(null);
    setElapsed(0);
    abort.current?.abort();
    abort.current = new AbortController();

    setFindingCandidates(true);
    try {
      const found = await findCandidates(
        name.trim(),
        company.trim(),
        lookingFor,
        abort.current.signal,
        useCache,
      );
      setCandidates(found);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setFindingCandidates(false);
    }
  }, [name, company, lookingFor, useCache]);

  /** Step two. Runs only once a human has pointed at a candidate. */
  const start = useCallback(async (confirmed: Candidate) => {
    setError(null);
    setRuns({});
    setLiveJob(null);
    setElapsed(0);
    abort.current?.abort();
    abort.current = new AbortController();

    // The confirmed candidate's own name is what gets researched: the user
    // may have typed a misspelling, or started from a company and picked a
    // person out of it.
    const subjectName = confirmed.name;

    // Sequential, not parallel. Both pipelines resolve the same identity and
    // discover the same companies, so the second reuses the first's cache
    // instead of paying for those fetches twice.
    const kinds: JobKind[] = mode === "both" ? ["screening", "network"] : [mode];
    setView(kinds[0]);

    try {
      for (const kind of kinds) {
        const startedAt = Date.now();
        const { job, report } = await runJob<ScreeningReport | NetworkReport>(
          kind,
          {
            name: subjectName,
            company: company.trim(),
            useFirecrawl,
            useCache,
            confirmed,
            engine,
          },
          setLiveJob,
          abort.current.signal,
        );
        setLiveJob(job);
        setRuns((previous) =>
          kind === "screening"
            ? { ...previous, screening: { job, report: report as ScreeningReport } }
            : { ...previous, network: { job, report: report as NetworkReport } },
        );
        if (kind === "screening") {
          // Kept per engine rather than per run, so re-running the same engine
          // replaces its own row and the other engine's result survives.
          const seconds = Math.round((Date.now() - startedAt) / 1000);
          setCompare((previous) => ({
            ...previous,
            [engine]: { job, report: report as ScreeningReport, seconds },
          }));
        }
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [mode, company, useFirecrawl, useCache, engine]);

  /**
   * Stop the run for real.
   *
   * Two things have to happen. Aborting the fetch stops this tab waiting; the
   * POST stops the worker, which is what actually stops Firecrawl credits and
   * OpenAI tokens being spent on a report nobody is going to read.
   */
  const stop = useCallback(async () => {
    const jobId = liveJob?.job_id;
    setStopping(true);
    abort.current?.abort();
    try {
      if (jobId) await cancelJob(jobId);
    } catch {
      // The run is already finishing, or the backend is gone. Either way the
      // user asked to stop, so the UI stops.
    } finally {
      setStopping(false);
      setLiveJob(null);
      setFindingCandidates(false);
      setError("Stopped. No further credits were spent.");
    }
  }, [liveJob]);

  const stepCaption =
    mode !== "both"
      ? undefined
      : both
        ? "Both reports finished."
        : runs.screening
          ? "Screening done. Running the network — step 2 of 2."
          : "Screening — step 1 of 2.";

  return (
    <>
      <header className="sticky top-0 z-10 border-b border-hairline bg-surface/85 backdrop-blur">
        <div className="mx-auto flex w-full max-w-5xl items-center justify-between gap-3 px-4 py-3.5 sm:px-6">
          <div className="flex items-center gap-2.5">
            <span className="grid h-7 w-7 place-items-center rounded-lg bg-brand text-[13px] font-bold text-white">
              A
            </span>
            <span className="text-[15px] font-bold tracking-tight text-title">Affluense</span>
          </div>

          <div className="flex items-center gap-2 text-[12px]">
            {healthError ? (
              <span className="rounded-full bg-danger-soft px-3 py-1 font-medium text-danger">
                Backend offline — run <code className="font-mono">uvicorn api:app</code>
              </span>
            ) : health ? (
              <>
                <span className="rounded-full bg-good-soft px-2.5 py-1 font-medium text-good">
                  connected
                </span>
                {health.firecrawl_configured && (
                  <span className="hidden rounded-full bg-sunken px-2.5 py-1 font-medium text-muted sm:inline">
                    Firecrawl
                  </span>
                )}
                {health.openai?.configured && (
                  <span
                    title={health.openai.note}
                    className="hidden rounded-full bg-sunken px-2.5 py-1 font-medium text-muted sm:inline"
                  >
                    {health.openai.model}
                  </span>
                )}
                {health.sentiment_degraded && (
                  <span className="rounded-full bg-warn-soft px-2.5 py-1 font-medium text-warn">
                    sentiment degraded
                  </span>
                )}
              </>
            ) : (
              <span className="text-faint">connecting…</span>
            )}
          </div>
        </div>
      </header>

      <main className="mx-auto w-full max-w-5xl flex-1 px-4 pb-16 pt-10 sm:px-6 sm:pt-14">
        <h1 className="max-w-[18ch] text-[38px] font-extrabold leading-[1.05] tracking-[-0.03em] text-title sm:text-[52px]">
          Know who you are onboarding.
        </h1>
        <p className="mt-4 max-w-[58ch] text-[16px] leading-relaxed text-body">
          Give us a name and one company they are connected to. We map the rest of their
          corporate footprint, read the public record on each company, and tell you what is
          worth a second look — with the source behind every claim.
        </p>

        <form
          onSubmit={(event) => {
            event.preventDefault();
            // Always the cheap step first. The expensive one is triggered by
            // picking a candidate, never by submitting this form.
            if (!running && !findingCandidates) void findWhoTheyMean();
          }}
          className="mt-8 rounded-2xl border border-hairline bg-surface p-5 shadow-[0_1px_2px_rgba(16,18,26,0.04),0_12px_28px_-18px_rgba(16,18,26,0.25)] sm:p-6"
        >
          <div
            role="group"
            aria-label="What are you searching for"
            className="mb-4 inline-flex rounded-lg bg-sunken p-1"
          >
            {(["person", "company"] as const).map((option) => (
              <button
                key={option}
                type="button"
                onClick={() => setLookingFor(option)}
                aria-pressed={lookingFor === option}
                className={`rounded-md px-3.5 py-1.5 text-[13px] font-semibold transition-all ${
                  lookingFor === option
                    ? "bg-surface text-title shadow-[0_1px_2px_rgba(16,18,26,0.10)]"
                    : "text-muted hover:text-title"
                }`}
              >
                {option === "person" ? "Search a person" : "Search a company"}
              </button>
            ))}
          </div>

          <div className="grid gap-4 sm:grid-cols-2">
            <label className="block">
              <span className="text-[13px] font-medium text-body">
                {lookingFor === "company" ? "Company" : "Individual"}
              </span>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                required
                minLength={2}
                placeholder={
                  lookingFor === "company"
                    ? "Company name, e.g. the registered entity"
                    : "Full name of the individual"
                }
                className="mt-1.5 w-full rounded-lg border border-edge bg-canvas px-3.5 py-2.5 text-[15px] text-title outline-none transition-colors placeholder:text-faint focus:border-brand focus:bg-surface focus:ring-4 focus:ring-brand/10"
              />
            </label>
            <label className={`block ${lookingFor === "company" ? "hidden sm:invisible sm:block" : ""}`}>
              <span className="text-[13px] font-medium text-body">Connected company</span>
              <input
                value={company}
                onChange={(e) => setCompany(e.target.value)}
                disabled={lookingFor === "company"}
                placeholder="Used to tell them apart from namesakes"
                className="mt-1.5 w-full rounded-lg border border-edge bg-canvas px-3.5 py-2.5 text-[15px] text-title outline-none transition-colors placeholder:text-faint focus:border-brand focus:bg-surface focus:ring-4 focus:ring-brand/10"
              />
            </label>
          </div>

          <div className="mt-5">
            <div
              role="group"
              aria-label="What to run"
              className="inline-flex w-full rounded-lg bg-sunken p-1 sm:w-auto"
            >
              {MODES.map((option) => {
                const active = mode === option.id;
                return (
                  <button
                    key={option.id}
                    type="button"
                    onClick={() => setMode(option.id)}
                    aria-pressed={active}
                    className={`flex-1 rounded-md px-4 py-2 text-[13px] font-semibold transition-all focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand sm:flex-none ${
                      active
                        ? "bg-surface text-title shadow-[0_1px_2px_rgba(16,18,26,0.10)]"
                        : "text-muted hover:text-title"
                    }`}
                  >
                    {option.label}
                  </button>
                );
              })}
            </div>
            <p className="mt-2.5 max-w-[64ch] text-[13px] text-muted">
              {MODES.find((option) => option.id === mode)?.blurb}
            </p>
          </div>

          <EngineSelector
            engine={engine}
            onChange={setEngine}
            disabled={running || findingCandidates}
            available={health?.engines}
            // Real timings from this subject's runs. They replace the
            // estimates on the buttons, so what is on screen is measured
            // rather than projected as soon as there is anything to measure.
            lastRun={{ v1: compare.v1?.seconds, v2: compare.v2?.seconds }}
          />

          <div className="mt-6 flex flex-wrap items-center gap-4 border-t border-hairline pt-5">
            <button
              type="submit"
              disabled={running || findingCandidates || !!healthError}
              className="rounded-lg bg-brand px-6 py-2.5 text-[14px] font-semibold text-white transition-colors hover:bg-brand-deep disabled:cursor-not-allowed disabled:opacity-40"
            >
              {running ? "Researching…" : findingCandidates ? "Searching…" : "Find"}
            </button>

            {(running || findingCandidates) && (
              <button
                type="button"
                onClick={() => void stop()}
                disabled={stopping}
                className="rounded-lg border border-edge px-4 py-2.5 text-[14px] font-medium text-muted transition-colors hover:border-danger hover:text-danger disabled:opacity-50"
              >
                {stopping ? "Stopping…" : "Stop"}
              </button>
            )}

            <label className="flex cursor-pointer items-center gap-2 text-[13px] text-body">
              <input
                type="checkbox"
                checked={useFirecrawl}
                onChange={(e) => setUseFirecrawl(e.target.checked)}
                disabled={!health?.firecrawl_configured}
                className="h-4 w-4 accent-brand"
              />
              Use Firecrawl
              {!health?.firecrawl_configured && (
                <span className="text-faint">(no key set)</span>
              )}
            </label>

            {SHOW_CACHE_TOGGLE && (
              <label className="flex cursor-pointer items-center gap-2 text-[13px] text-body">
                <input
                  type="checkbox"
                  checked={useCache}
                  onChange={(e) => setUseCache(e.target.checked)}
                  className="h-4 w-4 accent-brand"
                />
                Reuse cached data
                <span className="text-faint">
                  {useCache ? "(free, may be stale)" : "(fresh — spends credits)"}
                </span>
              </label>
            )}

            <span className="ml-auto text-[12px] text-faint">
              Finding candidates is free ·{" "}
              {engine === "v2"
                ? "agents research in parallel"
                : "a full run takes several minutes"}
            </span>
          </div>
        </form>

        {error && (
          <p className="mt-5 rounded-xl border border-danger/25 bg-danger-soft px-4 py-3 text-[14px] text-danger">
            {error}
          </p>
        )}

        {candidates && !liveJob && !shown && (
          <CandidatePicker
            result={candidates}
            busy={running}
            onSelect={(candidate) => void start(candidate)}
            onCancel={() => setCandidates(null)}
          />
        )}

        {liveJob && (
          <div className="mt-5">
            <RunStatus
              job={liveJob}
              elapsed={elapsed}
              caption={stepCaption}
              onStop={() => void stop()}
              stopping={stopping}
            />
          </div>
        )}

        {shown && (
          <div className="rise mt-8">
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
              {both ? (
                <div
                  role="group"
                  aria-label="Report"
                  className="inline-flex rounded-lg bg-sunken p-1"
                >
                  {(["screening", "network"] as const).map((tab) => (
                    <button
                      key={tab}
                      type="button"
                      onClick={() => setView(tab)}
                      aria-pressed={view === tab}
                      className={`rounded-md px-4 py-1.5 text-[13px] font-semibold transition-all ${
                        view === tab
                          ? "bg-surface text-title shadow-[0_1px_2px_rgba(16,18,26,0.10)]"
                          : "text-muted hover:text-title"
                      }`}
                    >
                      {tab === "screening" ? "Screening" : "Network"}
                    </button>
                  ))}
                </div>
              ) : (
                <h2 className="text-[20px] font-bold tracking-tight text-title">
                  {shown.job.kind === "screening" ? "Screening" : "Network"} ·{" "}
                  {shown.report.subject.resolved_name || shown.job.name}
                </h2>
              )}

              <a
                href={csvUrl(shown.job.job_id)}
                className="rounded-lg border border-edge px-3.5 py-2 text-[13px] font-medium text-body transition-colors hover:border-brand hover:text-brand"
              >
                Download CSV
              </a>
            </div>

            {view === "screening" && (compare.v1 || compare.v2) && (
              <div className="mb-5">
                <EngineCompare runs={compare} />
              </div>
            )}

            {view === "screening" && runs.screening ? (
              <ScreeningReportView report={runs.screening.report} />
            ) : runs.network ? (
              <NetworkView report={runs.network.report} />
            ) : null}

            {shown.report.notes.length > 0 && (
              <section className="mt-5 rounded-xl border border-hairline bg-surface p-5">
                <h3 className="text-[14px] font-bold tracking-tight text-title">
                  What the sources said
                </h3>
                <ul className="mt-2.5 space-y-1.5">
                  {shown.report.notes.map((note, index) => (
                    <li key={index} className="text-[13px] leading-relaxed text-muted">
                      {note}
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {shown.report.usage && (
              <CostPanel
                usage={shown.report.usage}
                identityUsage={candidates?.usage ?? null}
              />
            )}

            <p className="mt-5 max-w-[85ch] text-[12px] leading-relaxed text-faint">
              {shown.report.disclaimer}
            </p>
          </div>
        )}
      </main>
    </>
  );
}
