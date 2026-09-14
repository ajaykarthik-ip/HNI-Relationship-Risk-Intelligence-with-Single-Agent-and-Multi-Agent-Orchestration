import type { TimelineEntry } from "@/lib/api";
import { Card } from "./risk-primitives";

/**
 * Roles and events on one axis.
 *
 * Putting them together is the point. An adverse event sitting visibly before
 * a "role begins" marker is the IL&FS case made obvious — a fraud that
 * preceded the chairman appointed to clean it up — and no amount of prose
 * elsewhere makes that as clear as the order does.
 */
export function Timeline({ entries }: { entries: TimelineEntry[] }) {
  if (entries.length === 0) return null;

  return (
    <Card title="Timeline">
      <ol className="relative space-y-4 pl-5 before:absolute before:bottom-2 before:left-[3px] before:top-2 before:w-px before:bg-hairline">
        {entries.map((entry, index) => {
          const isFinding = entry.kind === "finding";
          const unattributed = isFinding && entry.attributed === false;
          const outside = isFinding && entry.within_tenure === false;

          return (
            <li key={`${index}-${entry.finding_id ?? entry.label}`} className="relative">
              <span
                aria-hidden
                className={`absolute -left-5 top-1.5 h-2.5 w-2.5 rounded-full border-2 ${
                  outside || unattributed
                    ? "border-edge bg-surface"
                    : isFinding
                      ? "border-danger bg-danger"
                      : "border-brand bg-surface"
                }`}
              />
              <p className="tabular font-mono text-[11px] tracking-wide text-faint">
                {entry.date ?? "undated"}
              </p>
              <p
                className={`text-[13.5px] font-semibold leading-snug ${
                  outside || unattributed ? "text-muted" : "text-title"
                }`}
              >
                {entry.label}
              </p>
              <p className="text-[12.5px] text-muted">
                {entry.entity}
                {entry.stage && ` · ${entry.stage}`}
                {entry.is_ongoing && " · ongoing"}
                {entry.detail && ` · ${entry.detail}`}
                {outside && " · outside the subject's tenure, not attributed"}
                {unattributed && !outside && " · not attributed to the subject"}
              </p>
            </li>
          );
        })}
      </ol>
    </Card>
  );
}
