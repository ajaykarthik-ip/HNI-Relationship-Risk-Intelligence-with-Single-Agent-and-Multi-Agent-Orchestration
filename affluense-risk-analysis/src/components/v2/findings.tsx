"use client";

import { useState } from "react";
import type { FindingRow } from "@/lib/api";
import {
  Badge, Card, STAGE_LABEL, Severity, TIER_LABEL, stageTone,
} from "./risk-primitives";

/**
 * One card per real-world event.
 *
 * Everything on the card exists to stop a reader over-reading it. The stage
 * says how far the matter actually went. The publisher count says how many
 * independent outlets said so — not how many articles exist, because ten
 * syndications of one wire story are one source. And anything that falls
 * outside the subject's time at the company is moved out of the main list
 * entirely rather than sitting among their findings with a caveat nobody
 * reads.
 */
export function Findings({
  findings,
  title = "Key findings",
  emptyNote,
}: {
  findings: FindingRow[];
  title?: string;
  emptyNote?: string;
}) {
  const material = findings.filter((f) => f.is_material);
  const excluded = findings.filter((f) => !f.is_material);

  if (findings.length === 0) {
    return (
      <Card title={title}>
        <div className="py-6 text-center">
          <span className="mx-auto grid h-10 w-10 place-items-center rounded-full bg-good-soft text-[18px] font-bold text-good">
            ✓
          </span>
          <p className="mt-3 text-[14px] font-semibold text-title">
            No adverse findings
          </p>
          <p className="mx-auto mt-1 max-w-[46ch] text-[13px] text-muted">
            {emptyNote ??
              "Nothing in the retrieved evidence describes an adverse event."}
          </p>
        </div>
      </Card>
    );
  }

  return (
    <Card
      title={title}
      bleed
      action={
        <span className="text-[12px] text-faint">
          {material.length} material
          {excluded.length > 0 && ` · ${excluded.length} not attributed`}
        </span>
      }
    >
      <ul>
        {material.map((finding) => (
          <FindingCard key={finding.event_id} finding={finding} />
        ))}
      </ul>

      {excluded.length > 0 && (
        <div className="border-t border-dashed border-edge bg-sunken px-4 py-3.5">
          <h4 className="text-[13px] font-semibold text-title">
            {excluded.length} item{excluded.length > 1 ? "s" : ""} found but not
            attributed to the subject
          </h4>
          <ul className="mt-2 space-y-1.5">
            {excluded.map((finding) => (
              <li key={finding.event_id} className="text-[12.5px] text-muted">
                <span className="font-medium text-body">{finding.entity}</span>
                {" — "}
                {finding.what_happened}
                {" · "}
                <span className="italic">{reasonExcluded(finding)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}

/** Why a finding is shown but not counted. Always stated, never implied. */
function reasonExcluded(finding: FindingRow): string {
  if (!finding.within_tenure)
    return "outside the subject's time at this company";
  if (!finding.attributed_to_subject)
    return "the subject does not answer for this organisation's conduct";
  if (finding.corroborating_publishers.length === 0)
    return "no identifiable publisher behind it";
  return "below the materiality threshold";
}

function FindingCard({ finding }: { finding: FindingRow }) {
  const [open, setOpen] = useState(false);

  return (
    <li className="border-b border-hairline last:border-b-0">
      <div className="px-4 py-4">
        <div className="flex flex-wrap items-baseline gap-2">
          <h4 className="min-w-[220px] flex-1 text-[15px] font-semibold leading-snug text-title">
            {finding.what_happened}
          </h4>
          <Badge tone={stageTone(finding.stage)}>
            {STAGE_LABEL[finding.stage] ?? finding.stage}
          </Badge>
          {finding.is_ongoing && <Badge tone="danger">ONGOING</Badge>}
        </div>

        <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-[12.5px] text-muted">
          <span className="font-medium text-body">{finding.entity}</span>
          {finding.entity_role && <span>· {finding.entity_role}</span>}
          {finding.event_date && <span>· {finding.event_date}</span>}
          <span>· </span>
          <Severity value={finding.severity} />
        </p>

        <p className="mt-2.5 text-[12.5px] text-muted">
          Reported independently by{" "}
          <strong className="text-title">
            {finding.corroborating_publishers.length}
          </strong>{" "}
          publisher
          {finding.corroborating_publishers.length === 1 ? "" : "s"}
          {finding.corroborating_publishers.length > 0 && (
            <> — {finding.corroborating_publishers.slice(0, 4).join(", ")}</>
          )}
          {finding.found_by && (
            <> · surfaced by the {finding.found_by.replace(/_/g, " ")} check</>
          )}
        </p>

        {finding.contradicting_sources.length > 0 && (
          <p className="mt-2 rounded-md bg-warn-soft px-3 py-2 text-[12.5px] text-warn">
            Sources disagree about this matter:{" "}
            {finding.contradicting_sources.slice(0, 3).join(" · ")}
          </p>
        )}
      </div>

      {finding.evidence.length > 0 && (
        <>
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            className="w-full border-t border-hairline px-4 py-2.5 text-left text-[12.5px] font-medium text-muted hover:text-title"
          >
            {open ? "▾" : "▸"} {finding.evidence.length} source
            {finding.evidence.length === 1 ? "" : "s"}
          </button>

          {open && (
            <ul className="space-y-3 bg-canvas/60 px-4 pb-4 pt-1">
              {finding.evidence.map((item, index) => (
                <li key={`${index}-${item.url}`}>
                  <div className="flex items-start justify-between gap-3">
                    <a
                      href={item.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-[13.5px] font-medium leading-snug text-title hover:text-brand hover:underline"
                    >
                      {item.headline ?? item.url}
                    </a>
                    <Badge tone={item.tier <= 1 ? "brand" : "muted"}>
                      {TIER_LABEL[item.tier] ?? "tier 3"}
                    </Badge>
                  </div>
                  {item.quote && (
                    <blockquote className="mt-1.5 border-l-2 border-edge pl-3 text-[13px] leading-relaxed text-body">
                      {item.quote}
                    </blockquote>
                  )}
                  <p className="tabular mt-1 font-mono text-[11px] text-faint">
                    {[item.publisher, item.published, fetchLabel(item.fetch_status)]
                      .filter(Boolean)
                      .join("  ·  ")}
                  </p>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </li>
  );
}

/** A claim read from an article and one inferred from a headline are not the
 *  same quality of evidence, and the reader has to be able to tell. */
function fetchLabel(status: string): string {
  return (
    {
      full: "full text read",
      partial: "partial text",
      paywalled: "paywalled — headline only",
      blocked: "not fetched (robots.txt)",
      failed: "could not fetch",
      headline_only: "headline only",
    }[status] ?? status
  );
}
