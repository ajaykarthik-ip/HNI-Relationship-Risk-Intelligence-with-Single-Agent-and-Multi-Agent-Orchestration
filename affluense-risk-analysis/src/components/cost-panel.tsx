import type { Usage } from "@/lib/api";

/**
 * What the run cost.
 *
 * Every number here arrives finished from the backend, which counts each
 * billable call as it is made and multiplies by a configured rate. This
 * component formats; it never calculates.
 *
 * Two things it must not do. It must not present trial credits as money — a
 * free trial that reports "$0.33" is a fabricated figure wearing a currency
 * symbol. And it must not omit the identity step: candidate ranking runs in a
 * separate request with its own meter, so its tokens are passed in here
 * explicitly rather than quietly dropped.
 */
export function CostPanel({
  usage,
  identityUsage,
}: {
  usage: Usage;
  identityUsage?: Usage | null;
}) {
  const { totals, rates, firecrawl } = usage;
  const onTrial = firecrawl?.plan === "trial";

  const identityTokens = identityUsage?.totals.total_tokens ?? 0;
  const identityUsd = identityUsage?.totals.openai_usd ?? 0;
  const tokens = totals.total_tokens + identityTokens;
  const usd = totals.usd + identityUsd;
  const inr = usd * rates.usd_to_inr;

  const money = (dollars: number) =>
    `$${dollars.toFixed(4)} · ₹${(dollars * rates.usd_to_inr).toLocaleString("en-IN", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    })}`;

  return (
    <section className="mt-5 rounded-xl border border-hairline bg-surface p-5">
      <div className="flex items-baseline justify-between gap-4">
        <h3 className="text-[14px] font-bold tracking-tight text-title">
          What this run cost
        </h3>
        {onTrial && (
          <span className="rounded-full bg-good-soft px-2.5 py-0.5 text-[11px] font-medium text-good">
            Firecrawl trial · no cash cost
          </span>
        )}
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-x-6 gap-y-2 sm:grid-cols-5">
        <Stat
          label="Requests"
          value={
            totals.served_from_cache > 0
              ? `${totals.requests.toLocaleString()} · ${totals.served_from_cache} cached`
              : totals.requests.toLocaleString()
          }
        />
        <Stat
          label={onTrial ? "Trial credits" : "Firecrawl credits"}
          value={
            firecrawl?.allowance
              ? `${totals.billable_credits.toLocaleString()} of ${firecrawl.allowance.toLocaleString()}`
              : totals.billable_credits.toLocaleString()
          }
        />
        <Stat
          label="Credits saved"
          value={totals.credits_saved_by_cache.toLocaleString()}
        />
        <Stat
          label="OpenAI tokens"
          value={tokens > 0 ? tokens.toLocaleString() : "none"}
        />
        <Stat label="Real spend" value={money(usd)} emphasis />
      </dl>

      {onTrial && (
        <p className="mt-3 text-[13px] leading-relaxed text-muted">
          {totals.billable_credits === 0 && totals.credits_saved_by_cache > 0 ? (
            <>
              No trial credits were spent — all{" "}
              {totals.served_from_cache.toLocaleString()} billable calls were
              answered from the local cache, avoiding{" "}
              {totals.credits_saved_by_cache.toLocaleString()} credits. Untick
              &ldquo;Reuse cached data&rdquo; to refetch.
            </>
          ) : (
            <>
              {totals.billable_credits.toLocaleString()} Firecrawl credits were
              consumed from your free trial, which costs no money
              {firecrawl?.runs_left_at_this_rate
                ? ` — roughly ${firecrawl.runs_left_at_this_rate} more runs at this rate.`
                : ". Set FIRECRAWL_TRIAL_CREDITS in .env to track what is left."}
              {totals.credits_saved_by_cache > 0 &&
                ` The cache avoided a further ${totals.credits_saved_by_cache.toLocaleString()}.`}
            </>
          )}
          {usd > 0 && ` The ${money(usd)} above is OpenAI, which is real spend.`}
        </p>
      )}

      {(tokens > 0 || totals.openai_calls > 0) && (
        <div className="mt-4 overflow-x-auto">
          <table className="w-full text-[12px] tabular-nums">
            <thead>
              <tr className="text-left text-faint">
                <th className="pb-1 font-medium">OpenAI used for</th>
                <th className="pb-1 text-right font-medium">Calls</th>
                <th className="pb-1 text-right font-medium">Tokens</th>
                <th className="pb-1 text-right font-medium">Cost</th>
              </tr>
            </thead>
            <tbody>
              {identityUsage?.openai_by_purpose?.map((row) => (
                <tr key={`identity-${row.purpose}`} className="border-t border-hairline">
                  <td className="py-1.5 text-muted">{row.purpose}</td>
                  <td className="py-1.5 text-right text-muted">{row.calls}</td>
                  <td className="py-1.5 text-right text-muted">
                    {(row.prompt + row.completion).toLocaleString()}
                  </td>
                  <td className="py-1.5 text-right text-muted">{money(row.usd)}</td>
                </tr>
              ))}
              {usage.openai_by_purpose?.map((row) => (
                <tr key={row.purpose} className="border-t border-hairline">
                  <td className="py-1.5 text-muted">{row.purpose}</td>
                  <td className="py-1.5 text-right text-muted">{row.calls}</td>
                  <td className="py-1.5 text-right text-muted">
                    {(row.prompt + row.completion).toLocaleString()}
                  </td>
                  <td className="py-1.5 text-right text-muted">{money(row.usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="mt-4 overflow-x-auto">
        <table className="w-full text-[12px] tabular-nums">
          <thead>
            <tr className="text-left text-faint">
              <th className="pb-1 font-medium">Service</th>
              <th className="pb-1 text-right font-medium">Calls</th>
              <th className="pb-1 text-right font-medium">Credits</th>
              <th className="pb-1 text-right font-medium">Cost</th>
            </tr>
          </thead>
          <tbody>
            {usage.by_host.map((row) => (
              <tr key={row.host} className="border-t border-hairline">
                <td className="py-1.5 text-muted">{row.host}</td>
                <td className="py-1.5 text-right text-muted">{row.calls}</td>
                <td className="py-1.5 text-right text-muted">
                  {row.credits > 0
                    ? row.credits.toLocaleString()
                    : row.cached_calls > 0
                      ? "cached"
                      : "free"}
                </td>
                <td className="py-1.5 text-right text-muted">
                  {row.credits > 0
                    ? onTrial
                      ? "trial credits"
                      : money(row.usd)
                    : row.cached_calls > 0
                      ? "nothing spent"
                      : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <p className="mt-3 text-[12px] leading-relaxed text-faint">
        {usage.note} Rates: ${rates.usd_per_mtok_in}/${rates.usd_per_mtok_out} per
        million OpenAI tokens in/out, 1 USD = ₹{rates.usd_to_inr.toFixed(2)} (
        {rates.usd_to_inr_source}).
        {inr > 0 && ` Total real spend this run: ₹${inr.toFixed(2)}.`}
      </p>
    </section>
  );
}

function Stat({
  label,
  value,
  emphasis,
}: {
  label: string;
  value: string;
  emphasis?: boolean;
}) {
  return (
    <div>
      <dt className="text-[11px] uppercase tracking-wide text-faint">{label}</dt>
      <dd
        className={`mt-0.5 tabular-nums ${
          emphasis ? "text-[15px] font-bold text-title" : "text-[14px] text-muted"
        }`}
      >
        {value}
      </dd>
    </div>
  );
}
