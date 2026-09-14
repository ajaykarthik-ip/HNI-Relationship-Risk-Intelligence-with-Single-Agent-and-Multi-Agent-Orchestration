/**
 * Client for the Affluense backend (FastAPI, see ../backend).
 *
 * Runs take 40-90 seconds, so the backend is submit-then-poll rather than
 * request-response. `runJob` hides that: it submits, polls, reports progress
 * as it arrives, and resolves with the finished report.
 */

const BASE =
  process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "") ?? "http://127.0.0.1:8000";

export type JobKind = "screening" | "network";
export type JobStatus = "queued" | "running" | "done" | "error" | "cancelled";
export type Sentiment = "positive" | "neutral" | "negative";

/**
 * Which backend engine runs the job.
 *
 * "v1" is the stable sequential pipeline and stays the default everywhere.
 * "v2" is the concurrent orchestration engine. Both produce the identical
 * report shape, which is what makes them comparable on one subject — so
 * nothing below this line needs to know which one ran.
 */
export type Engine = "v1" | "v2";

/** One stage of a V2 run. V1 jobs report `stages: null`. */
export interface StageProgress {
  stage: string;
  done: number;
  total: number;
  complete: boolean;
  elapsed_s: number;
}

export interface Job {
  job_id: string;
  kind: JobKind;
  /** Which engine ran it. Absent on a backend that predates the selector. */
  engine?: Engine;
  name: string;
  company: string | null;
  status: JobStatus;
  progress: string[];
  steps_completed: number;
  /**
   * How far through the run is, computed by the server.
   *
   * It has to be the server's number: `progress` carries only the last 20
   * lines, so a client counting phases in that window loses them entirely on
   * a long run and reports 2% while the run is two-thirds done.
   */
  percent?: number;
  phase?: string;
  created_at: string;
  finished_at: string | null;
  error: string | null;
  result_available: boolean;
  /**
   * Per-stage progress, V2 only.
   *
   * V1 infers its percentage by matching printed log lines, which cannot work
   * once several agents report at once — so V2 sends structured state instead
   * and this is it. Null for V1.
   */
  stages?: StageProgress[] | null;
}

export interface Health {
  status: string;
  sentiment_backend: string;
  sentiment_degraded: boolean;
  firecrawl_configured: boolean;
  openai: { configured: boolean; model: string | null; note: string };
  jobs: number;
  /** Engines this backend can run. Older backends omit it and mean ["v1"]. */
  engines?: Engine[];
  default_engine?: Engine;
}

/**
 * One possible answer to "who did you mean?".
 *
 * Produced by free sources only, before any credit is spent. `evidence` is
 * why this row is on the list, and is shown to the user rather than kept as
 * an internal score.
 */
export interface Candidate {
  id: string;
  name: string;
  kind: "person" | "company";
  description: string | null;
  extract: string | null;
  wikidata_id: string | null;
  wikipedia_url: string | null;
  thumbnail: string | null;
  score: number;
  evidence: string[];
  sources: string[];
  source_url: string | null;
  ranked_by: string;
}

export interface CandidateResponse {
  query: { name: string; company: string | null; mode: "person" | "company" };
  candidates: Candidate[];
  related_people: Candidate[];
  reasoning_layer: { configured: boolean; model: string | null; note: string };
  usage?: Usage;
  notes: string[];
}

export interface Subject {
  query_name: string;
  query_company: string | null;
  resolved_name: string | null;
  description: string | null;
  wikidata_id: string | null;
  wikipedia_url: string | null;
  match_confidence: number;
  match_basis: string;
  /** True when the backend could not confirm which person this is. */
  identity_unverified: boolean;
}

export interface Flag {
  category: string;
  stage: string;
  summary: string;
  first_reported: string | null;
  evidence: string[];
}

export interface Article {
  headline: string;
  url: string;
  publisher: string | null;
  published: string | null;
  sentiment: Sentiment;
  sentiment_score: number;
  risk_categories: string[];
  risk_stage: string | null;
  /**
   * Published outside the subject's time at the company. Shown as the
   * company's history, never counted as the subject's exposure.
   */
  outside_tenure?: boolean;
  tenure_note?: string | null;
}

export interface CompanyRow {
  company_name: string;
  sentiment: Sentiment;
  negative_news_flag: boolean;
  flag_categories: string[];
  flag_stages: string[];
  sentiment_breakdown: { negative: number; neutral: number; positive: number };
  articles_reviewed: number;
  insufficient_coverage: boolean;
  relationship: string[];
  /** control | public_office | employment | philanthropy | unknown */
  relationship_type: string;
  /** The finer class: current_company, board_seat, media_role, regulator… */
  relationship_class?: string;
  relationship_basis?: string;
  role_start?: number | null;
  role_end?: number | null;
  status: string;
  jurisdiction: string | null;
  registry_id: string | null;
  link_confidence: number;
  link_basis: string;
  aliases: string[];
  sources: string[];
  source_url: string | null;
  flags: Flag[];
  evidence: Article[];
  /** The last gate's verdict on this row. Deterministic, computed backend-side. */
  validation?: {
    status: "ok" | "uncertain" | "unverified" | "insufficient_coverage";
    issues: string[];
  };
}

/**
 * What the run cost, measured by the backend. Every figure here is a counter
 * multiplied by a rate in Python — nothing on this side does the arithmetic,
 * and nothing estimates it.
 */
export interface Usage {
  billing_unit: string;
  note: string;
  firecrawl?: {
    plan: "trial" | "paid";
    credits_this_run: number;
    allowance: number | null;
    usd: number;
    runs_left_at_this_rate?: number | null;
  };
  rates: {
    usd_per_credit: number;
    usd_per_mtok_in: number;
    usd_per_mtok_out: number;
    usd_to_inr: number;
    usd_to_inr_source: string;
  };
  totals: {
    requests: number;
    served_from_cache: number;
    billable_credits: number;
    credits_usd: number;
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
    openai_calls: number;
    openai_usd: number;
    usd: number;
    inr: number;
    credits_saved_by_cache: number;
    tokens_saved_by_cache: number;
    usd_saved_by_cache: number;
    inr_saved_by_cache: number;
  };
  openai_by_purpose?: {
    purpose: string;
    calls: number;
    prompt: number;
    completion: number;
    usd: number;
    inr: number;
  }[];
  by_host: {
    host: string;
    calls: number;
    cached_calls: number;
    credits: number;
    usd: number;
    inr: number;
  }[];
}

export type RiskLevel = "LOW" | "MEDIUM" | "HIGH" | "CRITICAL";
export type Confidence = "LOW" | "MEDIUM" | "HIGH";

/** The verdict. Computed by a deterministic rubric in Python, never by a model. */
export interface Assessment {
  risk_level: RiskLevel;
  confidence: Confidence;
  headline: string;
  reasons: { factor: string; detail: string; weight: number; finding_ids: string[] }[];
  confidence_basis: string[];
  limitations: string[];
  recommended_action: "proceed" | "review" | "escalate";
  material_findings: number;
  findings_considered: number;
  method: string;
}

export interface EvidenceItem {
  url: string;
  publisher: string;
  /** 0 promotional · 1 major · 2 established · 3 unknown */
  tier: number;
  published: string | null;
  headline: string | null;
  quote: string | null;
  fetch_status: string;
}

/**
 * One real-world event, assembled from every article describing it.
 *
 * `stage` and `attributed_to_subject` are the two fields that keep this
 * product defensible: an allegation is not a finding, and a company's history
 * is not a person's exposure.
 */
export interface FindingRow {
  event_id: string;
  what_happened: string;
  entity: string;
  entity_role: string;
  category: string;
  stage: string;
  event_date: string | null;
  is_ongoing: boolean;
  severity: number;
  confidence: number;
  attributed_to_subject: boolean;
  within_tenure: boolean;
  corroborating_publishers: string[];
  contradicting_sources: string[];
  evidence: EvidenceItem[];
  found_by: string | null;
  derived_from: string;
  is_material: boolean;
}

export interface TimelineEntry {
  date: string | null;
  kind: "role_start" | "role_end" | "finding";
  label: string;
  entity: string;
  detail?: string;
  stage?: string;
  is_ongoing?: boolean;
  attributed?: boolean;
  within_tenure?: boolean;
  finding_id: string | null;
}

/** What was actually searched. A clean verdict is only worth its search. */
export interface Coverage {
  groups_checked: string[];
  groups_available: string[];
  queries_issued: number;
  articles_retrieved: number;
  after_dedup: number;
  promotional_excluded: number;
  fulltext_attempted: number;
  fulltext_fetched: number;
  fulltext_paywalled: number;
  fulltext_blocked: number;
  articles_sent_to_model: number;
  articles_analysed_by_model: number;
  analysis_backend: string;
  publishers: number;
  by_tier: Record<string, number>;
}

export interface QcReport {
  issues: { severity: string; about: string; issue: string }[];
  reviewed_by: string;
  note: string;
}

export interface ScreeningReport {
  query: { individual: string; company: string | null; collected_at: string };
  subject: Subject;
  screening: CompanyRow[];
  investments: { entity: string; stake_or_amount: string | null; source_url: string }[];
  counts: Record<string, number>;
  assessment?: Assessment;
  findings?: FindingRow[];
  timeline?: TimelineEntry[];
  coverage?: Coverage;
  qc?: QcReport;
  usage?: Usage;
  validation?: {
    identity_confirmed: boolean;
    companies_by_status: Record<string, number>;
    attributable_findings: number;
    thresholds: Record<string, number>;
    explanation: string;
  };
  notes: string[];
  disclaimer: string;
}

export interface Suggestion {
  name: string;
  company: string | null;
  role: string | null;
  location: string | null;
  relevance_score: number;
  score_components: Record<string, number>;
  signals: string[];
  source_url: string | null;
  /** One-sentence argument written from the stored signals. */
  rationale?: string;
  /** False when the model judged the signals too weak to support the score. */
  rationale_supported?: boolean;
  rationale_by?: string;
}

export interface NetworkReport {
  /** Where the candidate pool came from. V2 only; V1 omits it. */
  candidate_sources?: { registry: number; news: number; filtered_out?: number };
  /**
   * Organisations that arrived as network members.
   *
   * Page extraction names companies as readily as people, so these are moved
   * out of the personal network rather than discarded — the affiliation is
   * real, it just isn't someone to meet.
   */
  network_affiliations?: {
    name: string;
    tie: string | null;
    source_url: string | null;
  }[];
  query: { individual: string; company: string | null; collected_at: string };
  subject: Subject;
  profile: { roles: string[]; industries: string[]; countries: string[]; companies: string[] };
  current_network: { name: string; tie: string; company: string | null; source_url: string }[];
  candidate_basis: string;
  suggested_connections: Suggestion[];
  scoring: { weights: Record<string, number>; explanation: string };
  counts: Record<string, number>;
  usage?: Usage;
  notes: string[];
  disclaimer: string;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      if (body?.detail) detail = typeof body.detail === "string" ? body.detail : detail;
    } catch {
      // Non-JSON error body; the status line is all we have.
    }
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

export const csvUrl = (jobId: string) => `${BASE}/api/jobs/${jobId}/result.csv`;

export const getHealth = () => request<Health>("/health");

/**
 * Stop a running job on the server.
 *
 * Aborting the browser's polling only stops the waiting. The worker keeps
 * going, and keeps spending credits and tokens on a report nobody will read,
 * until it is told to stop.
 */
export const cancelJob = (jobId: string) =>
  request<{ stopping: boolean; detail: string }>(`/api/jobs/${jobId}/cancel`, {
    method: "POST",
  });

/**
 * Step one: who did you mean?
 *
 * Free sources only, so this is safe to call on every search. It returns in
 * a couple of seconds and spends nothing.
 */
export const findCandidates = (
  name: string,
  company?: string,
  mode: "person" | "company" = "person",
  signal?: AbortSignal,
  useCache = true,
) =>
  request<CandidateResponse>("/api/candidates", {
    method: "POST",
    body: JSON.stringify({
      name,
      company: company || null,
      mode,
      use_cache: useCache,
    }),
    signal,
  });

export interface RunOptions {
  name: string;
  company?: string;
  useFirecrawl: boolean;
  /**
   * False refetches everything. News changes between runs, so a cached answer
   * can be stale — but a fresh run spends Firecrawl credits that a cached one
   * does not, which is why this is a choice rather than a default.
   */
  useCache?: boolean;
  maxCompanies?: number;
  maxNews?: number;
  maxSuggestions?: number;
  /** The candidate the user picked. Skips the backend's own identity guess. */
  confirmed?: Candidate | null;
  /** Which engine to run it on. Defaults to the stable V1 pipeline. */
  engine?: Engine;
}

/** Submit, poll, and resolve with the finished report. */
export async function runJob<T>(
  kind: JobKind,
  options: RunOptions,
  onProgress: (job: Job) => void,
  signal?: AbortSignal,
): Promise<{ job: Job; report: T }> {
  // Only the fields the backend's ConfirmedEntity model accepts. Sending the
  // whole candidate would push UI-only fields (score, evidence) into the API.
  const confirmed = options.confirmed
    ? {
        id: options.confirmed.id,
        name: options.confirmed.name,
        kind: options.confirmed.kind,
        wikidata_id: options.confirmed.wikidata_id,
        wikipedia_url: options.confirmed.wikipedia_url,
        description: options.confirmed.description,
      }
    : null;

  // Sent on both shapes. The backend defaults it to "v1", so omitting it is
  // the same as asking for the stable pipeline.
  const engine: Engine = options.engine ?? "v1";

  const payload =
    kind === "screening"
      ? {
          name: options.name,
          company: options.company || null,
          use_firecrawl: options.useFirecrawl,
          max_companies: options.maxCompanies ?? 8,
          max_news: options.maxNews ?? 20,
          use_cache: options.useCache ?? true,
          confirmed,
          engine,
        }
      : {
          name: options.name,
          company: options.company || null,
          use_firecrawl: options.useFirecrawl,
          max_suggestions: options.maxSuggestions ?? 20,
          use_cache: options.useCache ?? true,
          confirmed,
          engine,
        };

  let job = await request<Job>(`/api/${kind}`, {
    method: "POST",
    body: JSON.stringify(payload),
    signal,
  });
  onProgress(job);

  // Poll every 1.5s. Runs are minutes long; a tighter loop buys nothing.
  // The loop also ends on "cancelled", which the server sets when the run is
  // stopped from anywhere -- including another tab.
  while (job.status === "queued" || job.status === "running") {
    await new Promise((resolve) => setTimeout(resolve, 1500));
    if (signal?.aborted) throw new Error("Cancelled");
    job = await request<Job>(`/api/jobs/${job.job_id}`, { signal });
    onProgress(job);
  }

  if (job.status === "cancelled") throw new Error("Stopped.");
  if (job.status === "error") throw new Error(job.error ?? "The run failed");

  const report = await request<T>(`/api/jobs/${job.job_id}/result`, { signal });
  return { job, report };
}
