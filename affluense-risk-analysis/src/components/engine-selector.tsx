"use client";

import type { Engine } from "@/lib/api";

/**
 * Which execution engine runs the research.
 *
 * Deliberately built from the same segmented-control pattern as the mode
 * picker above it, because it is the same kind of choice and should not
 * announce itself as a new piece of machinery.
 *
 * Note the name: `src/components/v2/` already means "the redesigned UI" in
 * this project, which has nothing to do with the backend engine. Keeping this
 * file outside that folder, and calling the type `Engine`, stops the two
 * meanings of "v2" from colliding.
 */

const ENGINES: {
  id: Engine;
  label: string;
  /** Shown under the label until this engine has actually been timed here. */
  estimate: string;
  blurb: string;
}[] = [
  {
    id: "v1",
    label: "V1 · Single agent",
    // Measured on this pipeline: a full screening runs 500-600s.
    estimate: "8–10 min",
    blurb:
      "One agent working through the list in order. Proven and unchanged — the baseline every V2 run is measured against.",
  },
  {
    id: "v2",
    label: "V2 · Multi-agent",
    // Deliberately marked as a target, not a result. This is the figure the
    // architecture was designed to hit, and nothing has measured it yet on
    // this machine. It is replaced by the real number the moment a V2 run
    // finishes -- a projection printed as though it were a measurement is the
    // one number this product must never show.
    estimate: "~2 min*",
    blurb:
      "Six specialist agents — identity, discovery, evidence, full text, analysis and network — researching at the same time. Same sources, same queries, same risk scoring.",
  },
];

/** "1m 52s", or "48s" under a minute. */
function formatDuration(totalSeconds: number): string {
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return seconds ? `${minutes}m ${seconds}s` : `${minutes}m`;
}

export function EngineSelector({
  engine,
  onChange,
  disabled = false,
  available,
  lastRun,
}: {
  engine: Engine;
  onChange: (engine: Engine) => void;
  disabled?: boolean;
  /** Engines the backend reports. Undefined means only V1 is known. */
  available?: Engine[];
  /**
   * Seconds each engine actually took, from runs on this machine.
   *
   * Once an engine has a real number it replaces the estimate, and the caption
   * says the timing is measured rather than projected. Until then the figure
   * on V2 is labelled a target, because it is one.
   */
  lastRun?: Partial<Record<Engine, number>>;
}) {
  const offered = ENGINES.filter(
    (option) => !available || available.includes(option.id),
  );

  // Nothing to choose between: don't show a control that cannot do anything.
  if (offered.length < 2) return null;

  return (
    <div className="mt-5">
      <div
        role="group"
        aria-label="Which engine to run"
        className="inline-flex w-full flex-col gap-1 rounded-lg bg-sunken p-1 sm:w-auto sm:flex-row sm:gap-0"
      >
        {offered.map((option) => {
          const active = engine === option.id;
          const measured = lastRun?.[option.id];
          return (
            <button
              key={option.id}
              type="button"
              onClick={() => onChange(option.id)}
              aria-pressed={active}
              disabled={disabled}
              className={`flex-1 rounded-md px-5 py-2.5 text-left transition-all focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand disabled:cursor-not-allowed disabled:opacity-40 sm:flex-none sm:min-w-[14rem] ${
                active
                  ? "bg-surface text-title shadow-[0_1px_2px_rgba(16,18,26,0.10)]"
                  : "text-muted hover:text-title"
              }`}
            >
              <span className="flex items-baseline justify-between gap-3 whitespace-nowrap">
                <span className="text-[13px] font-semibold">
                  {option.label}
                </span>
                {/* Same line, quieter weight: the label is what you choose
                    between, the timing is what makes the choice obvious. */}
                <span
                  className={`tabular font-mono text-[11px] ${
                    measured
                      ? active
                        ? "text-brand"
                        : "text-muted"
                      : "text-faint"
                  }`}
                >
                  {measured ? formatDuration(measured) : option.estimate}
                </span>
              </span>
            </button>
          );
        })}
      </div>
      <p className="mt-2.5 max-w-[64ch] text-[13px] text-muted">
        {offered.find((option) => option.id === engine)?.blurb}
        {lastRun?.[engine] ? (
          <span className="text-body"> Timing above is your last run.</span>
        ) : engine === "v2" ? (
          <span className="text-faint">
            {" "}
            *Design target, not yet measured — replaced by the real figure once
            you run it.
          </span>
        ) : null}
      </p>
    </div>
  );
}

/** A small label for a finished or running report, so results stay traceable. */
export function EngineBadge({ engine }: { engine?: Engine }) {
  if (!engine) return null;
  const isV2 = engine === "v2";
  return (
    <span
      title={
        isV2
          ? "Multi-agent engine — six agents researching in parallel"
          : "Single agent — the stable sequential baseline"
      }
      className={`shrink-0 whitespace-nowrap rounded px-2 py-0.5 text-[11px] font-semibold tracking-wide ${
        isV2 ? "bg-brand/10 text-brand" : "bg-sunken text-muted"
      }`}
    >
      {isV2 ? "V2 · Multi-agent" : "V1 · Single agent"}
    </span>
  );
}
