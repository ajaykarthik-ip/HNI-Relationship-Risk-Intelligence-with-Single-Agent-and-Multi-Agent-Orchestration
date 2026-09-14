"use client";

import type { Job } from "@/lib/api";
import { EngineBadge } from "@/components/engine-selector";

/**
 * The backend reports the same step lines the CLI prints. A run takes a minute
 * or more, and an opaque wait that long reads as a hang, so the steps are shown
 * as they arrive — with a bar that says how far through the run is.
 *
 * The percentage is derived from which *phase* the run has reached, not from a
 * step count: the number of steps is not known until the companies have been
 * discovered, and a bar computed from "steps so far / steps guessed" jumps
 * backwards the moment the guess is wrong. Phases are known in advance and
 * always happen in the same order.
 */

/**
 * Phase boundaries, in the order the pipeline runs them. `upto` is the
 * percentage the bar has reached once that phase is complete. The widths are
 * roughly proportional to how long each phase actually takes: scraping and
 * screening dominate, identity resolution is almost instant.
 */
const PHASES: { match: RegExp; upto: number }[] = [
  { match: /resolving identity/i, upto: 8 },
  { match: /wikidata claims|company links/i, upto: 14 },
  { match: /co-officer/i, upto: 18 },
  { match: /firecrawl: search/i, upto: 24 },
  { match: /^\s*(search|read):/i, upto: 52 },
  { match: /company records merged/i, upto: 58 },
];

const SCREENING_FROM = 58;
const SCREENING_UPTO = 92;

/**
 * How far through the run the progress lines say we are.
 *
 * The server's figure wins whenever it sends one, because only the server has
 * the whole history: `progress` carries the last 20 lines, and on a run of a
 * hundred-plus steps the phase markers this function looks for have long since
 * scrolled out of that window. It then fell back to its floor and showed 2%
 * while the run was two-thirds done.
 *
 * What follows is the fallback for a backend that predates `percent`.
 */
function progressPercent(job: Job): number {
  if (typeof job.percent === "number") return job.percent;
  if (job.status === "done") return 100;
  if (job.status === "error" || job.status === "cancelled") return 100;
  if (job.progress.length === 0) return 2;

  // "14 company records merged to 7 entities" tells us how many companies
  // will be screened, which turns the longest phase into a real fraction.
  let expectedCompanies = 0;
  let screened = 0;
  for (const line of job.progress) {
    const merged = line.match(/merged to (\d+) entit/i);
    if (merged) expectedCompanies = Number(merged[1]);
    if (/^\s*screening:\s/i.test(line)) screened += 1;
  }

  if (expectedCompanies > 0 && screened > 0) {
    const share = Math.min(screened / expectedCompanies, 1);
    const base = SCREENING_FROM + (SCREENING_UPTO - SCREENING_FROM) * share;
    // Once every company has started, the person-level pass is what remains.
    const person = job.progress.some((l) => /screening the individual/i.test(l));
    return Math.min(person ? 97 : base, 97);
  }

  // Before the company count is known, position by the furthest phase reached.
  let percent = 2;
  for (const line of job.progress) {
    for (const phase of PHASES) {
      if (phase.match.test(line)) percent = Math.max(percent, phase.upto);
    }
  }
  return Math.min(percent, 97);
}

export function RunStatus({
  job,
  elapsed,
  caption,
  onStop,
  stopping,
}: {
  job: Job;
  elapsed: number;
  caption?: string;
  /** Stops the run on the server, not just the polling. */
  onStop?: () => void;
  stopping?: boolean;
}) {
  const running = job.status === "queued" || job.status === "running";
  const latest = job.progress[job.progress.length - 1];

  // Monotonic without needing to remember anything: the progress list only
  // grows, every phase takes the furthest match, and the screened count only
  // rises. A bar that slides backwards destroys the trust it exists to build.
  const percent = progressPercent(job);

  return (
    <div className="overflow-hidden rounded-xl border border-hairline bg-surface">
      <div
        className="h-1 bg-sunken"
        role="progressbar"
        aria-valuenow={Math.round(percent)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="Research progress"
      >
        <div
          className={`h-full transition-[width] duration-700 ease-out ${
            job.status === "error" ? "bg-danger" : "bg-brand"
          }`}
          style={{ width: `${percent}%` }}
        />
      </div>

      <div className="flex items-center gap-3 px-5 py-4">
        <span className="relative flex h-2.5 w-2.5 shrink-0">
          {running && (
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-brand opacity-60" />
          )}
          <span
            className={`relative inline-flex h-2.5 w-2.5 rounded-full ${
              running ? "bg-brand" : job.status === "error" ? "bg-danger" : "bg-good"
            }`}
          />
        </span>

        <div className="min-w-0 flex-1">
          <p className="flex items-center gap-2 text-[14px] font-medium text-title">
            {/* Which engine produced this, so a result is never ambiguous when
                the same subject has been run on both. */}
            <EngineBadge engine={job.engine} />
            <span className="truncate">
              {running
                ? latest || "Starting…"
                : job.status === "error"
                  ? "Failed"
                  : job.status === "cancelled"
                    ? "Stopped"
                    : "Finished"}
            </span>
          </p>
          <p className="mt-0.5 text-[12px] text-muted">
            {running && job.phase ? job.phase : caption}
            {running && job.phase && caption ? ` · ${caption}` : ""}
          </p>
        </div>

        <span className="tabular shrink-0 font-mono text-[13px] font-semibold text-title">
          {Math.round(percent)}%
        </span>
        <span className="tabular hidden shrink-0 font-mono text-[12px] text-faint sm:inline">
          {job.steps_completed} steps · {elapsed}s
        </span>

        {/* Next to the progress bar, because that is what someone watching a
            three-minute run is looking at. Buried in the form above, it was
            effectively not there. */}
        {running && onStop && (
          <button
            type="button"
            onClick={onStop}
            disabled={stopping}
            className="shrink-0 rounded-lg border border-edge px-3 py-1.5 text-[12.5px] font-semibold text-body transition-colors hover:border-danger hover:text-danger disabled:opacity-50"
          >
            {stopping ? "Stopping…" : "Stop"}
          </button>
        )}
      </div>

      {/* V2 reports which stages have finished and how long each took. V1
          sends nothing here and this simply does not render. */}
      {job.stages && job.stages.length > 0 && (
        <div className="flex flex-wrap gap-1.5 border-t border-hairline px-5 py-2.5">
          {job.stages.map((stage) => (
            <span
              key={stage.stage}
              title={`${stage.elapsed_s}s`}
              className={`rounded px-2 py-0.5 font-mono text-[11px] ${
                stage.complete
                  ? "bg-good/10 text-good"
                  : stage.done > 0
                    ? "bg-brand/10 text-brand"
                    : "bg-sunken text-faint"
              }`}
            >
              {stage.stage}
              {/* A finished stage with no countable work -- "starting", or a
                  stage that was skipped -- reads "0/1" if the fraction is
                  printed regardless. Show the count only while it means
                  something. */}
              {stage.complete
                ? ""
                : stage.total > 0
                  ? ` ${stage.done}/${stage.total}`
                  : ""}
            </span>
          ))}
        </div>
      )}

      {job.progress.length > 1 && (
        <details className="border-t border-hairline">
          <summary className="cursor-pointer px-5 py-2.5 text-[12px] font-medium text-muted hover:text-title">
            Show all steps
          </summary>
          <ol className="max-h-56 overflow-auto bg-canvas/70 px-5 py-3">
            {job.progress.map((line, index) => (
              <li key={`${index}-${line}`} className="font-mono text-[11px] leading-relaxed text-body">
                <span className="mr-2 text-faint">{String(index + 1).padStart(2, "0")}</span>
                {line}
              </li>
            ))}
          </ol>
        </details>
      )}
    </div>
  );
}
