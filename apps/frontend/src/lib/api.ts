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

export type WaNumberStatus =
  | "pending"
  | "qr_pending"
  | "connecting"
  | "connected"
  | "disconnected"
  | "logged_out";

export type WhatsAppNumber = {
  id: number;
  display_name: string;
  msisdn: string | null;
  session_id: string;
  status: WaNumberStatus;
  qr_data_url: string | null;
  last_seen_at: string | null;
  created_at: string;
};

export type WhatsAppGroup = {
  jid: string;
  subject: string;
  participants_count: number;
  announce: boolean;
};

export type BroadcastStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled";

export type BroadcastJob = {
  id: number;
  number_id: number;
  body: string;
  status: BroadcastStatus;
  total_targets: number;
  sent_count: number;
  failed_count: number;
  min_delay_seconds: number;
  max_delay_seconds: number;
  error: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
};

export type OutboundMessage = {
  id: number;
  jid: string;
  group_subject: string | null;
  status: "queued" | "sent" | "failed";
  error: string | null;
  attempted_at: string;
};

export type BroadcastDetail = BroadcastJob & {
  messages: OutboundMessage[];
};

export type BroadcastSummary = BroadcastJob & {
  number_display_name: string;
  number_msisdn: string | null;
};

export type BroadcastRequest = {
  body: string;
  targets: string[];
  min_delay_seconds: number;
  max_delay_seconds: number;
};

export type ChatKind = "dm" | "group";

export type Conversation = {
  id: number;
  number_id: number;
  number_display_name: string;
  jid: string;
  kind: ChatKind;
  name: string | null;
  last_message_at: string | null;
  last_message_preview: string | null;
  unread_count: number;
  tags: string[];
};

export type InboxTag = {
  id: number;
  name: string;
  color: string | null;
  conversations_count: number;
};

export type ConversationMessage = {
  id: number;
  direction: "in" | "out";
  sender_jid: string | null;
  sender_name: string | null;
  body: string;
  ts: string;
  status: string;
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
  runAutoTag: (id: number, opts: { only_untagged?: boolean; limit?: number } = {}) => {
    const params = new URLSearchParams();
    params.set("only_untagged", String(opts.only_untagged ?? true));
    if (opts.limit !== undefined) params.set("limit", String(opts.limit));
    return request<AutoTagResult>(
      `/campaigns/${id}/auto-tag?${params.toString()}`,
      { method: "POST" },
    );
  },
  runWaEnrichment: (
    id: number,
    opts: { only_unvalidated?: boolean; request_budget?: number; limit?: number } = {},
  ) => {
    const params = new URLSearchParams();
    if (opts.only_unvalidated !== undefined) {
      params.set("only_unvalidated", String(opts.only_unvalidated));
    }
    if (opts.request_budget !== undefined) {
      params.set("request_budget", String(opts.request_budget));
    }
    if (opts.limit !== undefined) {
      params.set("limit", String(opts.limit));
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
  listNumbers: () => request<WhatsAppNumber[]>(`/numbers`),
  getNumber: (id: number) => request<WhatsAppNumber>(`/numbers/${id}`),
  createNumber: (display_name: string) =>
    request<WhatsAppNumber>(`/numbers`, {
      method: "POST",
      body: JSON.stringify({ display_name }),
    }),
  deleteNumber: (id: number) =>
    request<{ ok: boolean }>(`/numbers/${id}`, { method: "DELETE" }),
  listNumberGroups: (numberId: number) =>
    request<WhatsAppGroup[]>(`/numbers/${numberId}/groups`),
  createBroadcast: (numberId: number, req: BroadcastRequest) =>
    request<BroadcastJob>(`/numbers/${numberId}/broadcast`, {
      method: "POST",
      body: JSON.stringify(req),
    }),
  listBroadcasts: (numberId: number) =>
    request<BroadcastJob[]>(`/numbers/${numberId}/broadcasts`),
  listAllBroadcasts: (opts: { limit?: number; status?: BroadcastStatus } = {}) => {
    const params = new URLSearchParams();
    if (opts.limit) params.set("limit", String(opts.limit));
    if (opts.status) params.set("status", opts.status);
    const qs = params.toString();
    return request<BroadcastSummary[]>(`/broadcasts${qs ? `?${qs}` : ""}`);
  },
  getBroadcast: (jobId: number) =>
    request<BroadcastDetail>(`/broadcasts/${jobId}`),
  listConversations: (
    opts: { number_id?: number; kind?: ChatKind; tag_ids?: number[] } = {},
  ) => {
    const params = new URLSearchParams();
    if (opts.number_id !== undefined) params.set("number_id", String(opts.number_id));
    if (opts.kind) params.set("kind", opts.kind);
    if (opts.tag_ids?.length) {
      for (const t of opts.tag_ids) params.append("tag_id", String(t));
    }
    const qs = params.toString();
    return request<Conversation[]>(`/conversations${qs ? `?${qs}` : ""}`);
  },
  listInboxTags: () => request<InboxTag[]>(`/inbox/tags`),
  createInboxTag: (name: string, color?: string) =>
    request<InboxTag>(`/inbox/tags`, {
      method: "POST",
      body: JSON.stringify({ name, color }),
    }),
  deleteInboxTag: (id: number) =>
    request<void>(`/inbox/tags/${id}`, { method: "DELETE" }),
  attachConversationTag: (convId: number, tagId: number) =>
    request<void>(`/conversations/${convId}/tags`, {
      method: "POST",
      body: JSON.stringify({ tag_id: tagId }),
    }),
  detachConversationTag: (convId: number, tagId: number) =>
    request<void>(`/conversations/${convId}/tags/${tagId}`, {
      method: "DELETE",
    }),
  listConversationMessages: (convId: number) =>
    request<ConversationMessage[]>(`/conversations/${convId}/messages`),
  sendReply: (convId: number, body: string) =>
    request<ConversationMessage>(`/conversations/${convId}/messages`, {
      method: "POST",
      body: JSON.stringify({ body }),
    }),
  markConversationRead: (convId: number) =>
    request<void>(`/conversations/${convId}/read`, { method: "POST" }),
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
