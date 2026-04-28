// Typed wrappers around the FastAPI backend. All types mirror the pydantic
// response models in apps/backend/src/main.py.

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export type CampaignSummary = {
  id: number;
  name: string;
  status: string;
  current_stage: string | null;
  leads_count: number;
  scored_leads_count: number;
  serper_credits_used: number;
  firecrawl_credits_used: number;
  created_at: string;
  completed_at: string | null;
};

export type CampaignStatus = {
  id: number;
  name: string;
  status: string;
  current_stage: string | null;
  industries: string[];
  locations: string[];
  negative_locations: string[];
  platforms: string[];
  serper_credits_used: number;
  max_credits_serper: number;
  firecrawl_credits_used: number;
  max_credits_firecrawl: number;
  queries_count: number;
  search_results_count: number;
  leads_count: number;
  scored_leads_count: number;
  created_at: string;
  completed_at: string | null;
};

export type ActivityRow = {
  search_result_id: number;
  url: string;
  title: string | null;
  status: string;
  fetch_strategy: string | null;
  query_text: string;
};

export type Lead = {
  id: number;
  invite_id: string;
  source_url: string | null;
  relevance: number | null;
  geo_fit: number | null;
  engagement: number | null;
  total_score: number | null;
  first_seen: string;
  last_seen: string;
};

export type CreateCampaignRequest = {
  name: string;
  industries: string[];
  locations: string[];
  negative_locations: string[];
  platforms: string[];
  max_credits_serper: number;
  max_credits_firecrawl: number;
};

export type CreateCampaignResponse = { id: number; status: string };

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`${API_URL}${path}`, {
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    ...init,
  });
  if (!r.ok) {
    let detail: unknown = await r.text();
    try {
      detail = JSON.parse(detail as string);
    } catch {
      /* leave as text */
    }
    throw new Error(`${r.status} ${r.statusText}: ${JSON.stringify(detail)}`);
  }
  return r.json() as Promise<T>;
}

// Used by SWR — the key IS the URL path.
export const fetcher = (path: string) => request(path);

export const api = {
  listCampaigns: () => request<CampaignSummary[]>("/campaigns"),
  getCampaign: (id: number) => request<CampaignStatus>(`/campaigns/${id}`),
  getActivity: (id: number, limit = 20) =>
    request<ActivityRow[]>(`/campaigns/${id}/activity?limit=${limit}`),
  getLeads: (id: number, opts: { limit?: number; only_scored?: boolean } = {}) => {
    const params = new URLSearchParams();
    if (opts.limit) params.set("limit", String(opts.limit));
    if (opts.only_scored) params.set("only_scored", "true");
    const qs = params.toString();
    return request<Lead[]>(`/campaigns/${id}/leads${qs ? `?${qs}` : ""}`);
  },
  createCampaign: (req: CreateCampaignRequest) =>
    request<CreateCampaignResponse>("/campaigns", {
      method: "POST",
      body: JSON.stringify(req),
    }),
};
