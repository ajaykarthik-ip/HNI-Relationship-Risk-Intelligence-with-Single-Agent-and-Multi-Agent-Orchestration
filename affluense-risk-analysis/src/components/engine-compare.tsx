"use client";

import type { Engine, Job, ScreeningReport } from "@/lib/api";

/**
 * V1 against V2 on the same subject.
 *
 * Appears only once both engines have produced a screening for the current
 * subject, and it deliberately shows coverage and verdict next to runtime
 * rather than runtime alone. A faster run that found less, or reached a
 * different risk level, is a regression — not a win — so the numbers that would
 * reveal that are given equal weight to the clock.
 *
 * The rigorous version of this lives in `backend/bench.py`, which controls
 * cache state and repeats each run. This is the at-a-glance version.
 */

export interface EngineRun {
  job: Job;
  report: ScreeningReport;
  seconds: number;
}

function seconds(run: EngineRun): number {
  if (run.seconds > 0) return run.seconds;
  const started = Date.parse(run.job.created_at);
  const finished = run.job.finished_at ? Date.parse(run.job.finished_at) : NaN;
  if (Number.isNaN(started) || Number.isNaN(finished)) return 0;
  return Math.max(0, Math.round((finished - started) / 1000));
}

type Row = {
  label: string;
  value: (run: EngineRun) => string;
  /** True when the two engines disagreeing is a correctness problem. */
  mustMatch?: boolean;
};

const ROWS: Row[] = [
  { label: "Runtime", value: (r) => `${seconds(r)}s` },
  {
    label: "Risk level",
    value: (r) => r.report.assessment?.risk_level ?? "—",
    mustMatch: true,
  },
  {
    label: "Confidence",
    value: (r) => r.report.assessment?.confidence ?? "—",
    mustMatch: true,
  },
  {
    label: "Findings",
    value: (r) => String(r.report.findings?.length ?? 0),
    mustMatch: true,
  },
  {
    label: "Queries issued",
    value: (r) => String(r.report.coverage?.queries_issued ?? 0),
    mustMatch: true,
  },
  {
    label: "Adverse checks",
    value: (r) => String(r.report.coverage?.groups_checked?.length ?? 0),
    mustMatch: true,
  },
  {
    label: "Articles kept",
    value: (r) => String(r.report.coverage?.after_dedup ?? 0),
  },
  {
    label: "Read in full",
    value: (r) => String(r.report.coverage?.fulltext_fetched ?? 0),
  },
  {
    label: "Companies screened",
    value: (r) => String(r.report.screening?.length ?? 0),
  },
  {
    label: "HTTP requests",
    value: (r) => String(r.report.usage?.totals?.requests ?? 0),
  },
  {
    label: "OpenAI calls",
    value: (r) => String(r.report.usage?.totals?.openai_calls ?? 0),
  },
  {
    label: "Total tokens",
    value: (r) => String(r.report.usage?.totals?.total_tokens ?? 0),
  },
  {
    label: "Firecrawl credits",
    value: (r) => String(r.report.usage?.totals?.billable_credits ?? 0),
  },
];

export function EngineCompare({
  runs,
}: {
  runs: Partial<Record<Engine, EngineRun>>;
}) {
  const v1 = runs.v1;
  const v2 = runs.v2;
  if (!v1 || !v2) return null;

  const v1Seconds = seconds(v1);
  const v2Seconds = seconds(v2);
  const speedup =
    v1Seconds > 0 && v2Seconds > 0 ? v1Seconds / v2Seconds : null;

  return (
    <section className="overflow-hidden rounded-xl border border-hairline bg-surface">
      <header className="flex flex-wrap items-baseline justify-between gap-2 border-b border-hairline px-5 py-3.5">
        <h2 className="text-[14px] font-semibold text-title">
          Single agent vs multi-agent — same subject, same inputs
        </h2>
        {speedup && (
          <span className="tabular font-mono text-[12.5px] text-muted">
            {speedup >= 1
              ? `V2 finished ${speedup.toFixed(1)}× faster`
              : `V2 was ${(1 / speedup).toFixed(1)}× slower`}
          </span>
        )}
      </header>

      <div className="overflow-x-auto">
        <table className="w-full min-w-[26rem] border-collapse text-[13px]">
          <thead>
            <tr className="border-b border-hairline text-left text-[12px] text-muted">
              <th className="px-5 py-2 font-medium">Measure</th>
              <th className="px-5 py-2 text-right font-medium">
                V1 · Single agent
              </th>
              <th className="px-5 py-2 text-right font-medium">
                V2 · Multi-agent
              </th>
            </tr>
          </thead>
          <tbody>
            {ROWS.map((row) => {
              const left = row.value(v1);
              const right = row.value(v2);
              // Coverage and verdict must agree. Flagging a mismatch here is
              // the point of the table: speed with a different answer is a
              // failure, not a result.
              const mismatch = row.mustMatch && left !== right;
              return (
                <tr
                  key={row.label}
                  className="border-b border-hairline/60 last:border-0"
                >
                  <td className="px-5 py-2 text-body">
                    {row.label}
                    {mismatch && (
                      <span
                        className="ml-2 rounded bg-danger/10 px-1.5 py-0.5 text-[10.5px] font-semibold uppercase text-danger"
                        title="These must agree between engines. Investigate before trusting the faster run."
                      >
                        differs
                      </span>
                    )}
                  </td>
                  <td className="tabular px-5 py-2 text-right font-mono text-title">
                    {left}
                  </td>
                  <td
                    className={`tabular px-5 py-2 text-right font-mono ${
                      mismatch ? "text-danger" : "text-title"
                    }`}
                  >
                    {right}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <p className="border-t border-hairline px-5 py-3 text-[12px] text-muted">
        Cache state is not controlled here, so a second run of the same subject
        is faster for both engines. For a fair measurement use{" "}
        <code className="font-mono text-[11.5px]">python bench.py</code>, which
        gives each engine its own cold cache.
      </p>
    </section>
  );
}
