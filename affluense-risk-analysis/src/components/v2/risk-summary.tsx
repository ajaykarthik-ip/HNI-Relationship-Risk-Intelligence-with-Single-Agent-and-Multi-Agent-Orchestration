"use client";

import { useState } from "react";
import type { Assessment, Coverage, Subject } from "@/lib/api";
import { ACTION_LABEL, RISK_TONE } from "./risk-primitives";

/**
 * The first screen. It exists to answer one question — should I spend more
 * time on this person? — before the reader scrolls anywhere.
 *
 * Two rules hold it together:
 *
 *   confidence sits beside risk, never under it. HIGH at high confidence and
 *   HIGH at low confidence call for different actions, and one number hides
 *   that.
 *
 *   a clean verdict shows its search. "No adverse findings" is only worth
 *   something if the reader can see how hard the tool looked, which is why
 *   the checks run appear here rather than in a footnote.
 */
export function RiskSummary({
  assessment,
  subject,
  coverage,
}: {
  assessment: Assessment;
  subject: Subject;
  coverage?: Coverage;
}) {
  const [showChecks, setShowChecks] = useState(false);
  const tone = RISK_TONE[assessment.risk_level];
  const clean = assessment.risk_level === "LOW";
  const checks = coverage?.groups_checked ?? [];

  return (
    <section
      className={`overflow-hidden rounded-xl border ${tone.border} bg-surface`}
    >
      <div className={`flex flex-wrap items-start gap-5 border-l-4 ${tone.border.replace("border-", "border-l-")} ${tone.bg} px-5 py-5`}>
        <div className="min-w-[240px] flex-1">
          <p className={`text-[30px] font-extrabold leading-none tracking-tight ${tone.text}`}>
            {assessment.risk_level} RISK
          </p>
          <p className="mt-2 max-w-[56ch] text-[15px] font-medium leading-snug text-title">
            {assessment.headline}
          </p>
          <p className="mt-1 text-[13px] text-muted">
            {subject.resolved_name ?? subject.query_name}
            {subject.description ? ` · ${subject.description}` : ""}
            {subject.identity_unverified
              ? " · identity not confirmed"
              : " · identity confirmed"}
          </p>
        </div>

        <div className="flex min-w-[150px] flex-col items-start gap-1 sm:items-end">
          <span className="text-[11px] uppercase tracking-wide text-faint">
            Confidence
          </span>
          <span className="text-[19px] font-bold leading-none text-title">
            {assessment.confidence}
          </span>
          <span className="mt-2 rounded-md border border-edge bg-surface px-2.5 py-1 text-[11.5px] font-semibold text-title">
            {ACTION_LABEL[assessment.recommended_action] ??
              assessment.recommended_action}
          </span>
        </div>
      </div>

      {assessment.reasons.length > 0 && (
        <div className="border-t border-hairline px-5 py-4">
          <h3 className="text-[11px] font-medium uppercase tracking-wide text-faint">
            {clean ? "What this means" : "Why this assessment"}
          </h3>
          <ul className="mt-3 space-y-2.5">
            {assessment.reasons.map((reason, index) => (
              <li key={index} className="flex gap-3">
                <span
                  aria-hidden
                  className={`mt-[7px] h-1.5 w-1.5 shrink-0 rounded-full ${tone.bar}`}
                />
                <span className="min-w-0">
                  <span className="block text-[14px] font-medium capitalize text-title">
                    {reason.factor.replace(/_/g, " ")}
                  </span>
                  <span className="block max-w-[76ch] text-[13px] leading-relaxed text-muted">
                    {reason.detail}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* A clean result has to show its search, or it is just an assertion. */}
      {clean && checks.length > 0 && (
        <div className="border-t border-hairline px-5 py-4">
          <button
            type="button"
            onClick={() => setShowChecks((open) => !open)}
            aria-expanded={showChecks}
            className="flex w-full items-center gap-2 text-left"
          >
            <span className="grid h-5 w-5 place-items-center rounded-full bg-good-soft text-[12px] font-bold text-good">
              ✓
            </span>
            <span className="flex-1 text-[13.5px] font-medium text-title">
              {checks.length} adverse checks run, none returned a material finding
            </span>
            <span className="text-[12px] text-brand">
              {showChecks ? "Hide" : "Show all"}
            </span>
          </button>

          {showChecks && (
            <ul className="mt-3 grid gap-1.5 sm:grid-cols-2">
              {checks.map((check) => (
                <li key={check} className="flex items-baseline gap-2 text-[13px]">
                  <span className="font-bold text-good">✓</span>
                  <span className="capitalize text-muted">
                    {check.replace(/_/g, " ")} — searched, nothing found
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <div className="grid gap-4 border-t border-hairline bg-sunken/60 px-5 py-4 sm:grid-cols-2">
        <div>
          <h3 className="text-[11px] font-medium uppercase tracking-wide text-faint">
            Confidence is {assessment.confidence.toLowerCase()} because
          </h3>
          <ul className="mt-2 space-y-1">
            {assessment.confidence_basis.map((line, index) => (
              <li key={index} className="text-[12.5px] leading-relaxed text-muted">
                · {line}
              </li>
            ))}
          </ul>
        </div>
        <div>
          <h3 className="text-[11px] font-medium uppercase tracking-wide text-faint">
            Limitations
          </h3>
          <ul className="mt-2 space-y-1">
            {assessment.limitations.map((line, index) => (
              <li key={index} className="text-[12.5px] leading-relaxed text-muted">
                · {line}
              </li>
            ))}
          </ul>
        </div>
      </div>

      <p className="border-t border-hairline px-5 py-2.5 text-[11.5px] leading-relaxed text-faint">
        {assessment.method}
      </p>
    </section>
  );
}
