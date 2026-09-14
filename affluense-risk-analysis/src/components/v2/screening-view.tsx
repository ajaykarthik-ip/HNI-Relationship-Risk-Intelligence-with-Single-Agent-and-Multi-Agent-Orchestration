"use client";

import { useState } from "react";
import type { CompanyRow, ScreeningReport } from "@/lib/api";
import { Pill, Stat, ToneBar } from "./primitives";
import { RELATIONSHIP_LABEL, attributes, stageTone } from "./risk-primitives";

// Fallback for reports produced before relationship_class existed.
const LEGACY_LABEL: Record<string, string> = {
  employment: "Past employer",
  philanthropy: "Non-profit",
  public_office: "Public appointment",
};

function CompanyCard({ row }: { row: CompanyRow }) {
  const [open, setOpen] = useState(false);
  const flagged = row.negative_news_flag;
  // Whether this company's conduct is the subject's own exposure. A former
  // employer's or a regulator's adverse news is shown, never as their finding.
  const attributable = row.relationship_class
    ? attributes(row.relationship_class)
    : row.relationship_type === "control" || row.relationship_type === "unknown";
  const alarming = flagged && attributable;
  const badge = row.relationship_class
    ? RELATIONSHIP_LABEL[row.relationship_class]
    : LEGACY_LABEL[row.relationship_type];
  // The last gate's verdict. Shown next to the claim it qualifies, never in a
  // footnote somewhere else on the page.
  const verdict = row.validation;

  return (
    <article
      className={`overflow-hidden rounded-xl border bg-surface transition-shadow ${
        alarming ? "border-danger/30" : "border-hairline"
      }`}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full flex-col gap-4 p-5 text-left hover:bg-canvas/60 focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-brand sm:flex-row sm:items-start sm:justify-between"
      >
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-[16px] font-bold tracking-tight text-title">
              {row.company_name}
            </h3>
            {row.status === "former" && <Pill tone="muted">former</Pill>}
            {badge && <Pill tone="muted">{badge}</Pill>}
            {verdict && verdict.status !== "ok" && (
              <Pill tone={verdict.status === "unverified" ? "warn" : "muted"}>
                {verdict.status.replace(/_/g, " ")}
              </Pill>
            )}
          </div>

          <p className="mt-1 text-[13px] text-body">
            {row.relationship.join(" · ") || "connected"}
          </p>
          <p className="tabular mt-1.5 font-mono text-[11px] text-faint">
            {[
              row.jurisdiction,
              row.registry_id,
              // The window the tenure filter used. Shown because it decides
              // which articles could be attributed at all.
              row.role_start
                ? `${row.role_start}–${row.role_end ?? "present"}`
                : null,
            ]
              .filter(Boolean)
              .join("  ") || "no registry id"}
          </p>

          {flagged && (
            <div className="mt-3 flex flex-wrap items-center gap-1.5">
              {row.flags.map((flag) => (
                <Pill key={flag.category} tone={attributable ? stageTone(flag.stage) : "muted"}>
                  <span className="capitalize">{flag.category}</span>
                  <span className="ml-1.5 opacity-70">{flag.stage}</span>
                </Pill>
              ))}
              {!attributable && (
                <span className="text-[11px] text-faint">
                  coverage of this organisation, not attributed to the subject
                </span>
              )}
            </div>
          )}
        </div>

        <div className="w-full shrink-0 sm:w-52">
          <div className="flex items-center justify-between gap-3">
            {row.insufficient_coverage ? (
              <Pill tone="muted">no data</Pill>
            ) : (
              <Pill
                tone={
                  row.sentiment === "negative"
                    ? "danger"
                    : row.sentiment === "positive"
                      ? "good"
                      : "muted"
                }
              >
                {row.sentiment}
              </Pill>
            )}
            <span className="tabular font-mono text-[11px] text-faint">
              {row.articles_reviewed} articles
            </span>
          </div>
          <div className="mt-2.5">
            <ToneBar counts={row.sentiment_breakdown} />
          </div>
          <p className="mt-2 text-[12px] font-medium text-brand">
            {open ? "Hide evidence" : "Show evidence"}
          </p>
        </div>
      </button>

      {open && (
        <div className="border-t border-hairline bg-canvas/70 px-5 py-5">
          <p className="max-w-[70ch] text-[13px] leading-relaxed text-body">
            Linked with{" "}
            <span className="tabular font-mono font-medium text-title">
              {Math.round(row.link_confidence * 100)}%
            </span>{" "}
            confidence. {row.link_basis}
            {row.aliases.length > 0 && ` Also seen as ${row.aliases.join(", ")}.`}
          </p>

          {verdict && verdict.issues.length > 0 && (
            <div className="mt-4 rounded-lg border-l-[3px] border-warn bg-warn-soft/50 px-4 py-3">
              <p className="text-[13px] font-semibold text-title">
                Read this row with care
              </p>
              <ul className="mt-1.5 space-y-1">
                {verdict.issues.map((issue, index) => (
                  <li
                    key={index}
                    className="max-w-[70ch] text-[13px] leading-relaxed text-body"
                  >
                    {issue}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {row.flags.map((flag) => (
            <div
              key={flag.category}
              className="mt-4 rounded-lg border-l-[3px] border-danger bg-danger-soft/60 px-4 py-3"
            >
              <p className="text-[13px] font-semibold capitalize text-title">
                {flag.category}
                <span className="ml-2 font-normal lowercase text-body">{flag.stage}</span>
              </p>
              <p className="mt-1 max-w-[70ch] text-[13px] leading-relaxed text-body">
                {flag.summary}
              </p>
            </div>
          ))}

          {row.evidence.length > 0 && (
            <ul className="mt-5 space-y-3.5">
              {row.evidence.map((article, index) => (
                <li key={`${index}-${article.url}`} className="flex gap-3">
                  <span
                    aria-hidden
                    className={`mt-2 h-2 w-2 shrink-0 rounded-full ${
                      article.sentiment === "negative"
                        ? "bg-danger"
                        : article.sentiment === "positive"
                          ? "bg-good"
                          : "bg-edge"
                    }`}
                  />
                  <div className="min-w-0">
                    <a
                      href={article.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-[14px] font-medium leading-snug text-title hover:text-brand"
                    >
                      {article.headline}
                    </a>
                    <p className="tabular mt-1 font-mono text-[11px] text-faint">
                      {[
                        article.publisher,
                        article.risk_categories.join(" "),
                        `${article.sentiment_score >= 0 ? "+" : ""}${article.sentiment_score.toFixed(2)}`,
                      ]
                        .filter(Boolean)
                        .join("  ·  ")}
                    </p>
                    {article.outside_tenure && (
                      <p className="mt-1 text-[11px] italic leading-relaxed text-faint">
                        {article.tenure_note ??
                          "outside the subject's time at this company"}
                        {" — the company's history, not attributed to them"}
                      </p>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </article>
  );
}

export function ScreeningView({ report }: { report: ScreeningReport }) {
  const rows = report.screening;
  const flagged = rows.filter(
    (row) =>
      row.negative_news_flag &&
      (row.relationship_type === "control" || row.relationship_type === "unknown"),
  );
  const confidence = Math.round(report.subject.match_confidence * 100);
  const unverified = report.subject.identity_unverified;

  // When the person could not be identified, nothing below is a finding
  // *about someone* — it is coverage matching a name. Saying otherwise is
  // how this tool would libel a stranger.
  const verdict = unverified
    ? `Could not confirm who this is, so nothing below is attributed to a person. ${flagged.length} of ${rows.length} companies matching this name carry adverse coverage.`
    : flagged.length === 0
      ? "Nothing adverse surfaced across the companies screened."
      : `${flagged.length} of ${rows.length} companies carry adverse coverage. None of it is a finding on its own — open a card to read the source.`;

  const banner = unverified
    ? "border-warn/30 bg-warn-soft/60"
    : flagged.length
      ? "border-danger/30 bg-danger-soft/40"
      : "border-good/25 bg-good-soft/50";

  const headline = unverified ? "text-warn" : flagged.length ? "text-danger" : "text-good";

  return (
    <div className="space-y-5">
      <div className={`rounded-xl border p-5 ${banner}`}>
        <p className={`text-[15px] font-semibold leading-snug ${headline}`}>{verdict}</p>
        <p className="mt-2 max-w-[75ch] text-[13px] leading-relaxed text-body">
          <span className="font-medium text-title">{report.subject.resolved_name}</span>
          {report.subject.description && ` — ${report.subject.description}.`}{" "}
          {unverified ? "Identity unverified." : `Identity matched at ${confidence}%.`}
        </p>
        {unverified && (
          <p className="mt-2 max-w-[75ch] rounded-lg bg-surface/80 px-3 py-2 text-[12px] leading-relaxed text-body">
            {report.subject.match_basis}
          </p>
        )}
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat value={rows.length} label="Companies" />
        <Stat value={report.counts.articles_reviewed ?? 0} label="Articles read" />
        <Stat
          value={flagged.length}
          label={unverified ? "Adverse (unattributed)" : "With adverse news"}
          tone={flagged.length ? (unverified ? "title" : "danger") : "good"}
        />
        <Stat value={report.investments.length} label="Investments" />
      </div>

      <div className="space-y-3">
        {rows.map((row, index) => (
          <CompanyCard key={`${row.company_name}-${row.registry_id ?? index}`} row={row} />
        ))}
      </div>

      {report.investments.length > 0 && (
        <section className="rounded-xl border border-hairline bg-surface p-5">
          <h3 className="text-[15px] font-bold tracking-tight text-title">Investments</h3>
          <ul className="mt-3 grid gap-2 sm:grid-cols-2">
            {report.investments.map((item) => (
              <li key={item.entity} className="flex items-baseline justify-between gap-3">
                <span className="text-[13px] text-body">{item.entity}</span>
                {item.stake_or_amount && (
                  <span className="tabular shrink-0 font-mono text-[12px] font-medium text-title">
                    {item.stake_or_amount}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
