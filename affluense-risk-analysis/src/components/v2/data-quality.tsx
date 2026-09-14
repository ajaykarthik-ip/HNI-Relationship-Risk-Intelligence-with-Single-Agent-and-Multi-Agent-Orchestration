import type { Coverage, QcReport } from "@/lib/api";
import { Badge, Card } from "./risk-primitives";

/**
 * What was actually searched, and what the tool is unsure about.
 *
 * This section is what makes a LOW verdict worth anything. A clean result
 * with no account of the search behind it is an assertion; one that can say
 * it ran fourteen adverse checks across two hundred queries and fifty
 * publishers is a finding.
 *
 * It also prints the review's own complaints about the report, which is
 * deliberate. A tool that hides its doubts is harder to trust than one that
 * lists them.
 */
export function DataQuality({
  coverage,
  qc,
}: {
  coverage?: Coverage;
  qc?: QcReport;
}) {
  if (!coverage && !qc) return null;

  return (
    <div className="grid gap-3 md:grid-cols-2">
      {coverage && <SearchPanel coverage={coverage} />}
      {qc && <ReviewPanel qc={qc} />}
    </div>
  );
}

function SearchPanel({ coverage }: { coverage: Coverage }) {
  const tiers = coverage.by_tier ?? {};
  const scored =
    (tiers["tier 1"] ?? 0) + (tiers["tier 2"] ?? 0) + (tiers["tier 3"] ?? 0);
  const share = (n: number) => (scored ? (n / scored) * 100 : 0);
  const missed = (coverage.groups_available ?? []).filter(
    (group) => !(coverage.groups_checked ?? []).includes(group),
  );

  return (
    <Card title="What was searched">
      <dl className="grid gap-1.5">
        <Row label="Adverse checks run" value={coverage.groups_checked.length} />
        <Row label="Queries issued" value={coverage.queries_issued} />
        <Row label="Articles retrieved" value={coverage.articles_retrieved} />
        <Row label="After de-duplication" value={coverage.after_dedup} />
        <Row label="Promotional excluded" value={coverage.promotional_excluded} />
        <Row label="Full articles read" value={coverage.fulltext_fetched} />
        <Row label="Distinct publishers" value={coverage.publishers} />
      </dl>

      {scored > 0 && (
        <>
          <div
            className="mt-4 flex h-1.5 overflow-hidden rounded-full bg-sunken"
            aria-hidden
          >
            <i className="block bg-brand" style={{ width: `${share(tiers["tier 1"] ?? 0)}%` }} />
            <i className="block bg-edge" style={{ width: `${share(tiers["tier 2"] ?? 0)}%` }} />
            <i className="block bg-sunken" style={{ width: `${share(tiers["tier 3"] ?? 0)}%` }} />
          </div>
          <div className="mt-2 flex flex-wrap gap-3 text-[11.5px] text-muted">
            <Legend colour="bg-brand" label="Major" value={tiers["tier 1"] ?? 0} />
            <Legend colour="bg-edge" label="Established" value={tiers["tier 2"] ?? 0} />
            <Legend colour="bg-sunken" label="Other" value={tiers["tier 3"] ?? 0} />
          </div>
        </>
      )}

      {(coverage.fulltext_paywalled > 0 || coverage.fulltext_blocked > 0) && (
        <p className="mt-3 text-[12.5px] leading-relaxed text-muted">
          {coverage.fulltext_paywalled > 0 &&
            `${coverage.fulltext_paywalled} article(s) were paywalled. `}
          {coverage.fulltext_blocked > 0 &&
            `${coverage.fulltext_blocked} could not be fetched because robots.txt disallows it. `}
          Findings resting on those use headline evidence only.
        </p>
      )}

      {missed.length > 0 && (
        <p className="mt-3 text-[12.5px] leading-relaxed text-warn">
          Not searched this run: {missed.join(", ").replace(/_/g, " ")}.
        </p>
      )}

      <p className="mt-3 text-[11.5px] leading-relaxed text-faint">
        Evidence analysed by {coverage.analysis_backend}. Absence of adverse
        evidence is not proof that none exists.
      </p>
    </Card>
  );
}

function ReviewPanel({ qc }: { qc: QcReport }) {
  return (
    <Card
      title="Review"
      action={<span className="text-[11.5px] text-faint">{qc.reviewed_by}</span>}
    >
      {qc.issues.length === 0 ? (
        <p className="text-[13px] text-muted">
          The review raised nothing. Every finding is backed by the evidence
          cited for it, and the stated confidence matches the coverage.
        </p>
      ) : (
        <ul className="space-y-2.5">
          {qc.issues.map((issue, index) => (
            <li key={index} className="flex gap-2.5">
              <Badge
                tone={
                  issue.severity === "high"
                    ? "danger"
                    : issue.severity === "medium"
                      ? "warn"
                      : "muted"
                }
              >
                {issue.severity}
              </Badge>
              <span className="min-w-0 flex-1">
                <span className="block text-[13px] leading-relaxed text-body">
                  {issue.issue}
                </span>
                <span className="tabular font-mono text-[11px] text-faint">
                  {issue.about}
                </span>
              </span>
            </li>
          ))}
        </ul>
      )}
      <p className="mt-3 text-[11.5px] leading-relaxed text-faint">{qc.note}</p>
    </Card>
  );
}

function Row({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex justify-between gap-4 text-[13px]">
      <dt className="text-muted">{label}</dt>
      <dd className="tabular font-semibold text-title">
        {value.toLocaleString()}
      </dd>
    </div>
  );
}

function Legend({
  colour,
  label,
  value,
}: {
  colour: string;
  label: string;
  value: number;
}) {
  return (
    <span className="flex items-center gap-1.5">
      <i className={`block h-2 w-2 rounded-[2px] ${colour}`} aria-hidden />
      {label} · <span className="tabular">{value}</span>
    </span>
  );
}
