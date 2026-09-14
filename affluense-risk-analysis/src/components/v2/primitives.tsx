"use client";

import type { ReactNode } from "react";

/** Small shared pieces, so a pill looks the same everywhere it appears. */

export function Pill({
  tone,
  children,
}: {
  tone: "danger" | "warn" | "good" | "muted" | "brand";
  children: ReactNode;
}) {
  const styles = {
    danger: "bg-danger-soft text-danger",
    warn: "bg-warn-soft text-warn",
    good: "bg-good-soft text-good",
    muted: "bg-sunken text-muted",
    brand: "bg-brand-soft text-brand-deep",
  } as const;

  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-1 text-[12px] font-medium ${styles[tone]}`}
    >
      {children}
    </span>
  );
}

export function Stat({
  value,
  label,
  tone = "title",
}: {
  value: string | number;
  label: string;
  tone?: "title" | "danger" | "good";
}) {
  const colour =
    tone === "danger" ? "text-danger" : tone === "good" ? "text-good" : "text-title";

  return (
    <div className="rounded-xl border border-hairline bg-surface px-4 py-3.5">
      <p className={`tabular text-[26px] font-bold leading-none tracking-tight ${colour}`}>
        {value}
      </p>
      <p className="mt-1.5 text-[12px] font-medium text-muted">{label}</p>
    </div>
  );
}

/**
 * Sentiment as a proportion bar. A company with 7 negative of 11 articles
 * reads very differently from one with 1 of 12, and a single coloured pill
 * hides that.
 */
export function ToneBar({
  counts,
}: {
  counts: { negative: number; neutral: number; positive: number };
}) {
  const total = counts.negative + counts.neutral + counts.positive;
  if (total === 0) {
    return <p className="text-[12px] text-faint">No coverage found</p>;
  }

  const segments = [
    { key: "negative", value: counts.negative, className: "bg-danger" },
    { key: "neutral", value: counts.neutral, className: "bg-edge" },
    { key: "positive", value: counts.positive, className: "bg-good" },
  ];

  return (
    <div>
      <div className="flex h-1.5 w-full overflow-hidden rounded-full bg-sunken">
        {segments.map(({ key, value, className }) =>
          value === 0 ? null : (
            <div key={key} className={className} style={{ width: `${(value / total) * 100}%` }} />
          ),
        )}
      </div>
      <p className="tabular mt-1.5 font-mono text-[11px] text-faint">
        {counts.negative} neg · {counts.neutral} neu · {counts.positive} pos
      </p>
    </div>
  );
}

/** The relevance score, as a number you can compare at a glance. */
export function ScoreDial({ value }: { value: number }) {
  const percent = Math.round(value * 100);
  return (
    <div className="flex items-center gap-3">
      <div className="relative h-11 w-11 shrink-0">
        <svg viewBox="0 0 36 36" className="h-11 w-11 -rotate-90">
          <circle cx="18" cy="18" r="15.5" fill="none" stroke="var(--color-sunken)" strokeWidth="4" />
          <circle
            cx="18"
            cy="18"
            r="15.5"
            fill="none"
            stroke="var(--color-brand)"
            strokeWidth="4"
            strokeLinecap="round"
            strokeDasharray={`${percent * 0.974} 100`}
          />
        </svg>
        <span className="tabular absolute inset-0 grid place-items-center font-mono text-[11px] font-medium text-title">
          {value.toFixed(2)}
        </span>
      </div>
    </div>
  );
}
