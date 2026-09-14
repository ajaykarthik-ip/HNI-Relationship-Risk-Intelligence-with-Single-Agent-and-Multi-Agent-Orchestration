"use client";

import type { NetworkReport } from "@/lib/api";
import { Pill, ScoreDial, Stat } from "./primitives";

export function NetworkView({ report }: { report: NetworkReport }) {
  const suggestions = report.suggested_connections;
  const weakBasis = report.candidate_basis === "occupation";

  return (
    <div className="space-y-5">
      <div className="rounded-xl border border-brand/20 bg-brand-soft/50 p-5">
        <p className="text-[15px] font-semibold leading-snug text-brand-deep">
          {suggestions.length === 0
            ? "No suggestions could be built — see the notes below for why."
            : `${suggestions.length} people worth meeting, ranked by how closely they match ${report.subject.resolved_name}'s existing network.`}
        </p>
        <div className="mt-3 flex flex-wrap gap-1.5">
          {report.profile.industries.slice(0, 4).map((industry) => (
            <Pill key={industry} tone="brand">
              {industry}
            </Pill>
          ))}
          {report.profile.roles.slice(0, 3).map((role) => (
            <Pill key={role} tone="muted">
              {role}
            </Pill>
          ))}
        </div>
        {weakBasis && (
          <p className="mt-3 max-w-[75ch] rounded-lg bg-surface/80 px-3 py-2 text-[12px] leading-relaxed text-warn">
            No industry could be established for this subject, so these are matched on shared
            occupation only. Treat them as weak suggestions — the scores say so too.
          </p>
        )}
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Stat value={report.counts.current_network ?? 0} label="Already connected" />
        <Stat value={report.counts.candidate_pool ?? 0} label="Candidates considered" />
        <Stat value={suggestions.length} label="Suggested" />
        <Stat value={report.profile.companies.length} label="Companies" />
      </div>

      {report.current_network.length > 0 && (
        <section className="rounded-xl border border-hairline bg-surface p-5">
          <h3 className="text-[15px] font-bold tracking-tight text-title">Already in the network</h3>
          <ul className="mt-3 grid gap-2.5 sm:grid-cols-2">
            {report.current_network.slice(0, 12).map((person, index) => (
              <li key={`${index}-${person.name}`} className="text-[13px]">
                <span className="font-medium text-title">{person.name}</span>
                <span className="text-muted"> — {person.tie}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <ol className="space-y-3">
        {suggestions.map((person, index) => (
          <li
            key={`${person.name}-${index}`}
            className="rounded-xl border border-hairline bg-surface p-5"
          >
            <div className="flex items-start gap-4">
              <span className="tabular mt-1 w-6 shrink-0 font-mono text-[13px] font-medium text-faint">
                {index + 1}
              </span>

              <div className="min-w-0 flex-1">
                <h3 className="text-[16px] font-bold tracking-tight text-title">{person.name}</h3>
                <p className="mt-0.5 text-[13px] text-body">
                  {[person.role, person.company, person.location].filter(Boolean).join(" · ")}
                </p>

                {person.rationale && (
                  <p
                    className={`mt-2.5 max-w-[70ch] text-[13.5px] leading-relaxed ${
                      person.rationale_supported === false
                        ? "text-muted italic"
                        : "text-title"
                    }`}
                  >
                    {person.rationale}
                    {person.rationale_supported === false && (
                      <span className="text-warn">
                        {" "}
                        — the signals behind this match are weak.
                      </span>
                    )}
                  </p>
                )}

                <ul className="mt-3 space-y-1.5">
                  {person.signals.map((signal) => (
                    <li
                      key={signal}
                      className="flex gap-2 text-[13px] leading-relaxed text-body"
                    >
                      <span aria-hidden className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-brand" />
                      {signal}
                    </li>
                  ))}
                </ul>

                <p className="tabular mt-3 font-mono text-[10px] text-faint">
                  {Object.entries(person.score_components)
                    .filter(([, value]) => value > 0)
                    .map(([key, value]) => `${key.replace(/_.*/, "")} ${value.toFixed(2)}`)
                    .join("   ")}
                </p>
              </div>

              <div className="shrink-0">
                <ScoreDial value={person.relevance_score} />
              </div>
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}
