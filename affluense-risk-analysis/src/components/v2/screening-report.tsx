"use client";

import { useState } from "react";
import type { ScreeningReport } from "@/lib/api";
import { DataQuality } from "./data-quality";
import { Findings } from "./findings";
import { Stat } from "./risk-primitives";
import { RiskSummary } from "./risk-summary";
import { ScreeningView } from "./screening-view";
import { Timeline } from "./timeline";

/**
 * The screening report, ordered for a decision rather than for a reader.
 *
 * The verdict comes first, then the findings behind it, then everything that
 * qualifies it. A reader who stops after the first screen should still have
 * the answer they came for; a reader who keeps going should be able to reach
 * the article a finding rests on in two clicks.
 *
 * Sections are tabs rather than one long scroll because this report is
 * scanned and operated, not read top to bottom, and a nine-section page
 * hides its own contents.
 */

type SectionId =
  | "overview"
  | "findings"
  | "companies"
  | "legal"
  | "timeline"
  | "quality";

export function ScreeningReportView({ report }: { report: ScreeningReport }) {
  const [section, setSection] = useState<SectionId>("overview");

  const assessment = report.assessment;
  const findings = report.findings ?? [];
  const legal = findings.filter((f) =>
    ["litigation", "regulatory", "investigation", "arrest", "sanctions"].includes(
      f.category,
    ),
  );
  const material = findings.filter((f) => f.is_material);

  // A report produced before the decision layer existed still renders, just
  // as the company view it always was.
  if (!assessment) return <ScreeningView report={report} />;

  const sections: { id: SectionId; label: string; count?: number }[] = [
    { id: "overview", label: "Overview" },
    { id: "findings", label: "Key findings", count: material.length },
    { id: "companies", label: "Companies", count: report.screening.length },
    { id: "legal", label: "Legal & regulatory", count: legal.length },
    { id: "timeline", label: "Timeline" },
    { id: "quality", label: "Data quality" },
  ];

  return (
    <div className="space-y-4">
      <RiskSummary
        assessment={assessment}
        subject={report.subject}
        coverage={report.coverage}
      />

      <div className="grid grid-cols-2 gap-2.5 sm:grid-cols-3 lg:grid-cols-6">
        <Stat label="Companies" value={report.screening.length} />
        <Stat label="Evidence items" value={report.coverage?.after_dedup ?? 0} />
        <Stat
          label="Adverse findings"
          value={material.length}
          tone={material.length > 0 ? "danger" : undefined}
        />
        <Stat label="Legal matters" value={legal.filter((f) => f.is_material).length} />
        <Stat label="Publishers" value={report.coverage?.publishers ?? 0} />
        <Stat
          label="Adverse checks"
          value={report.coverage?.groups_checked.length ?? 0}
        />
      </div>

      <nav
        aria-label="Report sections"
        className="flex gap-1 overflow-x-auto border-b border-hairline"
      >
        {sections.map((entry) => (
          <button
            key={entry.id}
            type="button"
            onClick={() => setSection(entry.id)}
            aria-current={section === entry.id ? "page" : undefined}
            className={`whitespace-nowrap border-b-2 px-3.5 py-2.5 text-[13px] transition-colors ${
              section === entry.id
                ? "border-brand font-semibold text-title"
                : "border-transparent font-medium text-muted hover:text-title"
            }`}
          >
            {entry.label}
            {entry.count !== undefined && (
              <span className="tabular ml-1.5 text-faint">{entry.count}</span>
            )}
          </button>
        ))}
      </nav>

      {section === "overview" && (
        <div className="space-y-4">
          <Findings
            findings={material.slice(0, 3)}
            title="Most significant findings"
            emptyNote="No adverse event was found that is attributable to the subject within their time at any connected company."
          />
          <DataQuality coverage={report.coverage} qc={report.qc} />
        </div>
      )}

      {section === "findings" && (
        <Findings
          findings={findings}
          emptyNote="No adverse event was found that is attributable to the subject within their time at any connected company."
        />
      )}

      {section === "companies" && <ScreeningView report={report} />}

      {section === "legal" && (
        <Findings
          findings={legal}
          title="Legal & regulatory matters"
          emptyNote="No litigation, enforcement, regulatory or insolvency matter was found for the subject or their connected companies."
        />
      )}

      {section === "timeline" && <Timeline entries={report.timeline ?? []} />}

      {section === "quality" && (
        <DataQuality coverage={report.coverage} qc={report.qc} />
      )}
    </div>
  );
}
