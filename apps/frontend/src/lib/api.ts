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

export type LeadStatus =
  | "pending"
  | "approved"
  | "rejected"
  | "joined"
  | "contacted"
  | "archived";

export const LEAD_STATUSES: LeadStatus[] = [
  "pending",
  "approved",
  "rejected",
  "joined",
  "contacted",
  "archived",
];

export type StatusCounts = Record<LeadStatus, number>;

export type Lead = {
  id: number;
  invite_id: string;
  source_url: string | null;
  relevance: number | null;
  geo_fit: number | null;
  engagement: number | null;
  total_score: number | null;
  status: LeadStatus;
  tags: string[];
  verified_group_name: string | null;
  verified_group_description: string | null;
  last_validated_at: string | null;
  first_seen: string;
  last_seen: string;
};

export type Tag = {
  id: number;
  name: string;
  color: string | null;
  leads_count: number | null;
};

export type AutoTagResult = {
  considered: number;
  tagged: number;
  tags_created: number;
  links_created: number;
  business_attached: number;
  business_detached: number;
  misc_attached: number;
  misc_detached: number;
};

export type EnrichmentResult = {
  considered: number;
  fetched: number;
  valid: number;
  invalid: number;
  transient_errors: number;
};

export type FilteredLead = {
  id: number;
  invite_id: string;
  source_url: string | null;
  relevance: number | null;
  geo_fit: number | null;
  engagement: number | null;
  total_score: number | null;
  reason: string;
  group_name: string | null;
};

export type FilterResponse = {
  prompt: string;
  total_considered: number;
  matched_count: number;
  dropped_invalid: number;
  leads: FilteredLead[];
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
  getLeads: (
    id: number,
    opts: {
      limit?: number;
      only_scored?: boolean;
      status?: LeadStatus;
      tag_id?: number[];
    } = {},
  ) => {
    const params = new URLSearchParams();
    if (opts.limit) params.set("limit", String(opts.limit));
    if (opts.only_scored) params.set("only_scored", "true");
    if (opts.status) params.set("status", opts.status);
    if (opts.tag_id?.length) {
      for (const t of opts.tag_id) params.append("tag_id", String(t));
    }
    const qs = params.toString();
    return request<Lead[]>(`/campaigns/${id}/leads${qs ? `?${qs}` : ""}`);
  },
  listTags: (id: number) => request<Tag[]>(`/campaigns/${id}/tags`),
  runAutoTag: (id: number, only_untagged = true) =>
    request<AutoTagResult>(
      `/campaigns/${id}/auto-tag?only_untagged=${only_untagged}`,
      { method: "POST" },
    ),
  runWaEnrichment: (id: number, opts: { only_unvalidated?: boolean; request_budget?: number } = {}) => {
    const params = new URLSearchParams();
    if (opts.only_unvalidated !== undefined) {
      params.set("only_unvalidated", String(opts.only_unvalidated));
    }
    if (opts.request_budget !== undefined) {
      params.set("request_budget", String(opts.request_budget));
    }
    const qs = params.toString();
    return request<EnrichmentResult>(
      `/campaigns/${id}/enrich-whatsapp${qs ? `?${qs}` : ""}`,
      { method: "POST" },
    );
  },
  getStatusCounts: (id: number) =>
    request<StatusCounts>(`/campaigns/${id}/leads/status-counts`),
  updateLeadStatus: (campaignId: number, leadId: number, status: LeadStatus) =>
    request<Lead>(`/campaigns/${campaignId}/leads/${leadId}/status`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    }),
  deleteDeadLeads: (campaignId: number) =>
    request<{ deleted: number }>(`/campaigns/${campaignId}/leads/dead`, {
      method: "DELETE",
    }),
  createCampaign: (req: CreateCampaignRequest) =>
    request<CreateCampaignResponse>("/campaigns", {
      method: "POST",
      body: JSON.stringify(req),
    }),
  filterLeads: (
    id: number,
    prompt: string,
    opts: { only_scored?: boolean; status?: LeadStatus | null } = {},
  ) =>
    request<FilterResponse>(`/campaigns/${id}/filter`, {
      method: "POST",
      body: JSON.stringify({
        prompt,
        only_scored: opts.only_scored ?? true,
        status: opts.status === undefined ? "pending" : opts.status,
      }),
    }),
};
