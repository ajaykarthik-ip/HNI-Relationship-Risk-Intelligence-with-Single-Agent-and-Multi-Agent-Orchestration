import type { ReactNode } from "react";
import type { Confidence, RiskLevel } from "@/lib/api";

/**
 * Shared vocabulary for the decision layer.
 *
 * Risk colour is semantic and deliberately separate from the brand accent:
 * a reader must be able to tell severity from hue without reading a word,
 * and an indigo "HIGH" would say nothing.
 */

export const RISK_TONE: Record<RiskLevel, {
  text: string; bg: string; border: string; bar: string;
}> = {
  LOW: {
    text: "text-good", bg: "bg-good-soft", border: "border-good/30",
    bar: "bg-good",
  },
  MEDIUM: {
    text: "text-warn", bg: "bg-warn-soft", border: "border-warn/30",
    bar: "bg-warn",
  },
  HIGH: {
    text: "text-danger", bg: "bg-danger-soft", border: "border-danger/30",
    bar: "bg-danger",
  },
  CRITICAL: {
    text: "text-danger", bg: "bg-danger-soft", border: "border-danger/50",
    bar: "bg-danger",
  },
};

export const ACTION_LABEL: Record<string, string> = {
  proceed: "Proceed",
  review: "Review recommended",
  escalate: "Escalate — do not proceed without sign-off",
};

/** Human wording for a relationship class. */
export const RELATIONSHIP_LABEL: Record<string, string> = {
  current_company: "Current company",
  former_company: "Former company",
  executive_role: "Executive role",
  former_employer: "Former employer",
  employment: "Employment",
  board_seat: "Board seat",
  investment: "Investment",
  subsidiary: "Subsidiary",
  parent: "Parent company",
  nonprofit: "Non-profit",
  think_tank: "Think tank",
  regulator: "Regulator",
  government_body: "Public body",
  trade_association: "Industry body",
  media_role: "Media appearance",
  alias: "Alias",
  other: "Unclassified",
};

/** Which relationships carry the subject's own exposure. Mirrors the backend. */
const ATTRIBUTES = new Set([
  "current_company", "former_company", "executive_role", "board_seat", "control",
]);

export const attributes = (relationshipClass?: string) =>
  ATTRIBUTES.has(relationshipClass ?? "");

export const STAGE_LABEL: Record<string, string> = {
  reported: "Reported",
  alleged: "Alleged",
  investigating: "Under investigation",
  charged: "Charged",
  settled: "Settled",
  dismissed: "Dismissed",
  convicted: "Convicted",
};

/** Stages where the matter is unresolved, so the badge should read hot. */
const OPEN_STAGES = new Set(["alleged", "investigating", "charged"]);

export function stageTone(stage: string): "danger" | "warn" | "good" | "muted" {
  if (stage === "convicted") return "danger";
  if (OPEN_STAGES.has(stage)) return stage === "charged" ? "danger" : "warn";
  if (stage === "dismissed") return "good";
  return "muted";
}

export const TIER_LABEL: Record<number, string> = {
  0: "promotional", 1: "tier 1", 2: "tier 2", 3: "tier 3",
};

export function Badge({
  tone = "muted",
  children,
}: {
  tone?: "danger" | "warn" | "good" | "muted" | "brand";
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
      className={`inline-flex items-center whitespace-nowrap rounded px-2 py-0.5 font-mono text-[10.5px] font-medium tracking-wide ${styles[tone]}`}
    >
      {children}
    </span>
  );
}

export function Card({
  title,
  action,
  children,
  bleed,
}: {
  title: string;
  action?: ReactNode;
  children: ReactNode;
  /** Let the content run to the card's edges, for tables and lists. */
  bleed?: boolean;
}) {
  return (
    <section className="overflow-hidden rounded-xl border border-hairline bg-surface">
      <header className="flex items-center gap-3 border-b border-hairline px-4 py-3">
        <h3 className="flex-1 text-[14px] font-bold tracking-tight text-title">
          {title}
        </h3>
        {action}
      </header>
      <div className={bleed ? "" : "px-4 py-4"}>{children}</div>
    </section>
  );
}

/** Severity as five bars. Reads at a glance; the number is in the title. */
export function Severity({ value }: { value: number }) {
  return (
    <span
      className="inline-flex items-center gap-[2px]"
      title={`Severity ${value} of 5`}
      aria-label={`Severity ${value} of 5`}
    >
      {[1, 2, 3, 4, 5].map((step) => (
        <i
          key={step}
          className={`block h-1 w-2.5 rounded-[1px] ${
            step <= value
              ? value >= 4
                ? "bg-danger"
                : value >= 3
                  ? "bg-warn"
                  : "bg-edge"
              : "bg-sunken"
          }`}
        />
      ))}
    </span>
  );
}

export function Stat({
  label,
  value,
  tone,
}: {
  label: string;
  value: string | number;
  tone?: "danger" | "muted";
}) {
  return (
    <div className="rounded-xl border border-hairline bg-surface px-4 py-3">
      <div
        className={`tabular text-[21px] font-bold leading-tight ${
          tone === "danger" ? "text-danger" : "text-title"
        }`}
      >
        {value}
      </div>
      <div className="text-[11.5px] text-faint">{label}</div>
    </div>
  );
}
