"use client";

import type { Candidate, CandidateResponse } from "@/lib/api";

/**
 * "Who did you mean?" — the step between typing a name and spending money.
 *
 * Every row states why it is on the list, because the user is being asked to
 * make the call the tool used to make silently. A confident-looking list with
 * no reasoning would just move the guess, not remove it.
 */
export function CandidatePicker({
  result,
  onSelect,
  onCancel,
  busy,
}: {
  result: CandidateResponse;
  onSelect: (candidate: Candidate) => void;
  onCancel: () => void;
  busy: boolean;
}) {
  const { candidates, related_people: related, query } = result;
  const nothingFound = candidates.length === 0 && related.length === 0;

  return (
    <section className="mt-8 rounded-2xl border border-hairline bg-surface p-5 sm:p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-[20px] font-bold tracking-tight text-title">
            Which {query.mode === "company" ? "company" : "one"} did you mean?
          </h2>
          <p className="mt-1 max-w-[62ch] text-[13px] leading-relaxed text-muted">
            Nothing has been researched yet and no credits have been spent. Pick the
            right {query.mode === "company" ? "entity" : "person"} and the deep
            research starts.
          </p>
        </div>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-lg border border-edge px-3 py-1.5 text-[13px] font-medium text-muted transition-colors hover:bg-sunken"
        >
          Change search
        </button>
      </div>

      {nothingFound ? (
        <p className="mt-5 rounded-xl bg-sunken p-4 text-[13px] leading-relaxed text-muted">
          No free source returned a match for{" "}
          <strong className="text-title">{query.name}</strong>. Check the spelling, or
          add a connected company to narrow the search.
        </p>
      ) : (
        <ul className="mt-5 space-y-2.5">
          {candidates.map((candidate) => (
            <CandidateRow
              key={candidate.id}
              candidate={candidate}
              onSelect={onSelect}
              busy={busy}
            />
          ))}
        </ul>
      )}

      {related.length > 0 && (
        <div className="mt-6">
          <h3 className="text-[14px] font-bold tracking-tight text-title">
            People connected to {query.name}
          </h3>
          <p className="mt-1 text-[13px] text-muted">
            Screening is always about a person, so pick whose footprint you want.
          </p>
          <ul className="mt-3 space-y-2.5">
            {related.map((candidate) => (
              <CandidateRow
                key={candidate.id}
                candidate={candidate}
                onSelect={onSelect}
                busy={busy}
              />
            ))}
          </ul>
        </div>
      )}

      <p className="mt-5 text-[12px] leading-relaxed text-faint">
        {result.reasoning_layer.note}
      </p>
    </section>
  );
}

function CandidateRow({
  candidate,
  onSelect,
  busy,
}: {
  candidate: Candidate;
  onSelect: (candidate: Candidate) => void;
  busy: boolean;
}) {
  return (
    <li>
      <button
        type="button"
        disabled={busy}
        onClick={() => onSelect(candidate)}
        className="group flex w-full items-start gap-3.5 rounded-xl border border-hairline bg-canvas p-4 text-left transition-colors hover:border-brand hover:bg-surface disabled:cursor-not-allowed disabled:opacity-60"
      >
        {candidate.thumbnail ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img
            src={candidate.thumbnail}
            alt=""
            className="h-11 w-11 shrink-0 rounded-full object-cover"
          />
        ) : (
          <span className="grid h-11 w-11 shrink-0 place-items-center rounded-full bg-sunken text-[15px] font-bold text-muted">
            {candidate.name.slice(0, 1)}
          </span>
        )}

        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-2">
            <span className="text-[15px] font-bold text-title">{candidate.name}</span>
            {candidate.wikidata_id && (
              <span className="rounded-full bg-sunken px-2 py-0.5 text-[11px] font-medium text-muted">
                {candidate.wikidata_id}
              </span>
            )}
            <span className="rounded-full bg-sunken px-2 py-0.5 text-[11px] font-medium text-muted">
              {candidate.kind}
            </span>
          </span>

          {candidate.description && (
            <span className="mt-0.5 block text-[13px] text-body">
              {candidate.description}
            </span>
          )}

          <span className="mt-2 block space-y-0.5">
            {candidate.evidence.slice(0, 3).map((line, index) => (
              <span key={index} className="block text-[12px] leading-relaxed text-muted">
                · {line}
              </span>
            ))}
          </span>

          <span className="mt-1.5 block text-[11px] text-faint">
            {candidate.sources.join(", ")} · ranked by {candidate.ranked_by}
          </span>
        </span>

        <span className="shrink-0 self-center text-[13px] font-medium text-brand opacity-0 transition-opacity group-hover:opacity-100">
          Research →
        </span>
      </button>
    </li>
  );
}
