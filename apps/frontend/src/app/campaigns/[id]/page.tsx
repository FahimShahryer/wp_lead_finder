"use client";

import Link from "next/link";
import { use, useState } from "react";
import useSWR, { mutate } from "swr";
import {
  Archive,
  ArrowLeft,
  Check,
  CheckCheck,
  ChevronLeft,
  ExternalLink,
  MessageCircle,
  RotateCcw,
  X,
} from "lucide-react";

import {
  ActivityRow,
  CampaignStatus,
  LEAD_STATUSES,
  Lead,
  LeadStatus,
  StatusCounts,
  Tag,
  api,
  fetcher,
} from "@/lib/api";
import { inviteUrl } from "@/lib/invite-url";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { StatusBadge } from "@/components/status-badge";
import { PlatformBadge } from "@/components/platform-badge";
import { CategorizePanel } from "@/components/categorize-panel";
import { ExportPanel } from "@/components/export-panel";

const TERMINAL = new Set(["done", "budget_exceeded", "failed"]);

function StatStat({
  label,
  value,
  hint,
}: {
  label: string;
  value: React.ReactNode;
  hint?: string;
}) {
  return (
    <div className="space-y-1">
      <div className="text-xs uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className="text-2xl font-semibold tabular-nums">{value}</div>
      {hint && <div className="text-xs text-muted-foreground">{hint}</div>}
    </div>
  );
}

const ACTIVITY_VARIANT: Record<string, "default" | "secondary" | "outline" | "success" | "warning" | "destructive"> = {
  new: "secondary",
  fetched: "success",
  fetch_failed: "destructive",
  extracted: "default",
};

function ActivityBadge({ status }: { status: string }) {
  // @ts-expect-error narrow above
  return <Badge variant={ACTIVITY_VARIANT[status] || "outline"}>{status}</Badge>;
}

type Transition = {
  next: LeadStatus;
  label: string;
  icon: React.ReactNode;
  variant?: "outline" | "ghost" | "destructive" | "default";
};

// Forward chain: pending → approved → joined → contacted → archived.
// Reject branches off pending or approved. Every stage exposes both a forward
// step (where the lifecycle naturally goes) and a backward step (to undo).
const TRANSITIONS: Record<LeadStatus, Transition[]> = {
  pending: [
    { next: "approved", label: "Approve", icon: <Check className="h-3.5 w-3.5" />, variant: "outline" },
    { next: "rejected", label: "Reject", icon: <X className="h-3.5 w-3.5" />, variant: "ghost" },
  ],
  approved: [
    { next: "pending", label: "Back to pending", icon: <ChevronLeft className="h-3.5 w-3.5" />, variant: "ghost" },
    { next: "joined", label: "Mark joined", icon: <CheckCheck className="h-3.5 w-3.5" />, variant: "outline" },
    { next: "rejected", label: "Reject", icon: <X className="h-3.5 w-3.5" />, variant: "ghost" },
  ],
  rejected: [
    { next: "pending", label: "Back to pending", icon: <ChevronLeft className="h-3.5 w-3.5" />, variant: "ghost" },
    { next: "approved", label: "Approve", icon: <Check className="h-3.5 w-3.5" />, variant: "outline" },
  ],
  joined: [
    { next: "approved", label: "Back to approved", icon: <ChevronLeft className="h-3.5 w-3.5" />, variant: "ghost" },
    { next: "contacted", label: "Mark contacted", icon: <MessageCircle className="h-3.5 w-3.5" />, variant: "outline" },
  ],
  contacted: [
    { next: "joined", label: "Back to joined", icon: <ChevronLeft className="h-3.5 w-3.5" />, variant: "ghost" },
    { next: "archived", label: "Archive", icon: <Archive className="h-3.5 w-3.5" />, variant: "outline" },
  ],
  archived: [
    { next: "contacted", label: "Back to contacted", icon: <ChevronLeft className="h-3.5 w-3.5" />, variant: "ghost" },
    { next: "pending", label: "Reset to pending", icon: <RotateCcw className="h-3.5 w-3.5" />, variant: "ghost" },
  ],
};

function LeadActions({
  lead,
  busy,
  onChange,
}: {
  lead: Lead;
  busy: boolean;
  onChange: (next: LeadStatus) => void;
}) {
  const buttons = TRANSITIONS[lead.status];
  return (
    <div className="inline-flex gap-1">
      {buttons.map((b) => (
        <Button
          key={b.next}
          size="sm"
          variant={b.variant ?? "outline"}
          disabled={busy}
          onClick={() => onChange(b.next)}
          aria-label={b.label}
          title={b.label}
          className="h-7 px-2"
        >
          {b.icon}
          <span className="sr-only">{b.label}</span>
        </Button>
      ))}
    </div>
  );
}

export default function CampaignPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const cid = Number(id);

  // Default to "all" — leads start untriaged and live in the All bucket
  // until manually approved/rejected/etc.
  const [statusFilter, setStatusFilter] = useState<LeadStatus | "all">("all");
  const [tagFilter, setTagFilter] = useState<number[]>([]);
  const [pendingLeadId, setPendingLeadId] = useState<number | null>(null);
  const [autoTagBusy, setAutoTagBusy] = useState(false);
  const [autoTagMsg, setAutoTagMsg] = useState<string | null>(null);
  const [autoTagLimit, setAutoTagLimit] = useState<number>(10);
  const [enrichBusy, setEnrichBusy] = useState(false);
  const [enrichMsg, setEnrichMsg] = useState<string | null>(null);
  const [enrichLimit, setEnrichLimit] = useState<number>(10);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteMsg, setDeleteMsg] = useState<string | null>(null);

  const { data: campaign } = useSWR<CampaignStatus>(`/campaigns/${cid}`, fetcher, {
    refreshInterval: (latest) =>
      latest && TERMINAL.has(latest.status) ? 0 : 2000,
  });

  const { data: activity } = useSWR<ActivityRow[]>(
    `/campaigns/${cid}/activity?limit=20`,
    fetcher,
    { refreshInterval: campaign && TERMINAL.has(campaign.status) ? 0 : 2000 },
  );

  const tagFilterQs = tagFilter.map((t) => `&tag_id=${t}`).join("");
  const statusQs = statusFilter === "all" ? "" : `&status=${statusFilter}`;
  const leadsKey = `/campaigns/${cid}/leads?limit=100${statusQs}${tagFilterQs}`;
  const { data: leads } = useSWR<Lead[]>(leadsKey, fetcher, {
    refreshInterval: campaign && TERMINAL.has(campaign.status) ? 0 : 5000,
  });

  const countsKey = `/campaigns/${cid}/leads/status-counts`;
  const { data: counts } = useSWR<StatusCounts>(countsKey, fetcher, {
    refreshInterval: campaign && TERMINAL.has(campaign.status) ? 0 : 5000,
  });

  const tagsKey = `/campaigns/${cid}/tags`;
  const { data: tags } = useSWR<Tag[]>(tagsKey, fetcher);

  function toggleTagFilter(id: number) {
    setTagFilter((prev) =>
      prev.includes(id) ? prev.filter((t) => t !== id) : [...prev, id],
    );
  }

  async function runAutoTag() {
    setAutoTagBusy(true);
    setAutoTagMsg(null);
    try {
      const res = await api.runAutoTag(cid, { only_untagged: true, limit: autoTagLimit });
      const bucketNote =
        res.business_attached || res.business_detached || res.misc_attached || res.misc_detached
          ? ` · biz ±${res.business_attached}/-${res.business_detached} · misc ±${res.misc_attached}/-${res.misc_detached}`
          : "";
      setAutoTagMsg(
        `Tagged ${res.tagged} of ${res.considered} leads · ${res.tags_created} new tag${res.tags_created === 1 ? "" : "s"}${bucketNote}`,
      );
      await Promise.all([mutate(tagsKey), mutate(leadsKey)]);
    } catch (e) {
      setAutoTagMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setAutoTagBusy(false);
    }
  }

  async function deleteDeadLeads() {
    if (
      !confirm(
        "Permanently delete every lead whose invite has been verified as dead? This also removes their tags. This cannot be undone.",
      )
    ) {
      return;
    }
    setDeleteBusy(true);
    setDeleteMsg(null);
    try {
      const res = await api.deleteDeadLeads(cid);
      setDeleteMsg(
        res.deleted === 0 ? "No dead leads to delete." : `Deleted ${res.deleted} dead lead${res.deleted === 1 ? "" : "s"}.`,
      );
      await Promise.all([mutate(leadsKey), mutate(countsKey), mutate(tagsKey)]);
    } catch (e) {
      setDeleteMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setDeleteBusy(false);
    }
  }

  async function runEnrichment() {
    setEnrichBusy(true);
    setEnrichMsg(null);
    try {
      const res = await api.runWaEnrichment(cid, { only_unvalidated: true, limit: enrichLimit });
      const transientNote =
        res.transient_errors > 0 ? ` · ${res.transient_errors} transient` : "";
      setEnrichMsg(
        `Enriched ${res.fetched}/${res.considered} · valid ${res.valid} · dead ${res.invalid}${transientNote}`,
      );
      await Promise.all([mutate(leadsKey), mutate(countsKey)]);
    } catch (e) {
      // 503 (block) and 400 (budget) come back from the API as Error messages
      // — surface them prominently so the user can react (cooldown / proxy /
      // bigger budget) rather than silently failing.
      setEnrichMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setEnrichBusy(false);
    }
  }

  async function setStatus(leadId: number, next: LeadStatus) {
    setPendingLeadId(leadId);
    try {
      await api.updateLeadStatus(cid, leadId, next);
      // Re-fetch the current leads view + the counts row.
      await Promise.all([mutate(leadsKey), mutate(countsKey)]);
    } finally {
      setPendingLeadId(null);
    }
  }

  if (!campaign) return <div className="text-muted-foreground">Loading campaign…</div>;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <Link href="/" className="text-xs text-muted-foreground hover:underline">
            <ArrowLeft className="mr-1 inline h-3 w-3" />
            All campaigns
          </Link>
          <h1 className="mt-1 flex flex-wrap items-center gap-3 text-2xl font-semibold tracking-tight">
            {campaign.name}
            <span className="text-base font-normal text-muted-foreground">#{campaign.id}</span>
            <PlatformBadge platform={campaign.platform} />
            <StatusBadge status={campaign.status} stage={campaign.current_stage} />
          </h1>
          <p className="text-sm text-muted-foreground">
            {campaign.industries.join(", ") || "—"} · in{" "}
            {campaign.locations.join(", ") || "any geo"}
            {campaign.negative_locations.length > 0 && (
              <> · excl {campaign.negative_locations.join(", ")}</>
            )}
          </p>
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Pipeline progress</CardTitle>
          <CardDescription>
            {TERMINAL.has(campaign.status) ? "Run finished." : "Auto-refresh every 2 seconds."}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 gap-6 sm:grid-cols-4 lg:grid-cols-6">
            <StatStat label="Queries" value={campaign.queries_count} />
            <StatStat label="Search results" value={campaign.search_results_count} />
            <StatStat label="Leads" value={campaign.leads_count} />
            <StatStat
              label="Scored"
              value={campaign.scored_leads_count}
              hint={`of ${campaign.leads_count}`}
            />
            <StatStat
              label="Serper"
              value={`${campaign.serper_credits_used}/${campaign.max_credits_serper}`}
            />
            <StatStat
              label="Firecrawl"
              value={`${campaign.firecrawl_credits_used}/${campaign.max_credits_firecrawl}`}
            />
          </div>
        </CardContent>
      </Card>

      <CategorizePanel campaignId={cid} platform={campaign.platform} />

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="text-lg">Live activity</CardTitle>
            <CardDescription>
              Most recent search results processed (newest first).
            </CardDescription>
          </CardHeader>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>URL</TableHead>
                  <TableHead className="w-32">Strategy</TableHead>
                  <TableHead className="w-32">Status</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {activity && activity.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={3} className="py-6 text-center text-muted-foreground">
                      No activity yet.
                    </TableCell>
                  </TableRow>
                )}
                {activity?.map((row) => (
                  <TableRow key={row.search_result_id}>
                    <TableCell className="max-w-[280px] truncate">
                      <a
                        href={row.url}
                        target="_blank"
                        rel="noreferrer"
                        className="hover:underline"
                        title={row.title || row.url}
                      >
                        {row.title || row.url}
                      </a>
                      <div className="truncate text-xs text-muted-foreground">{row.url}</div>
                    </TableCell>
                    <TableCell>
                      {row.fetch_strategy ? (
                        <Badge variant="outline">{row.fetch_strategy}</Badge>
                      ) : (
                        <span className="text-xs text-muted-foreground">—</span>
                      )}
                    </TableCell>
                    <TableCell>
                      <ActivityBadge status={row.status} />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-start justify-between gap-2 space-y-0">
            <div>
              <CardTitle className="text-lg">Leads</CardTitle>
              <CardDescription>Move leads through the lifecycle. Ranked by total score within each tab.</CardDescription>
            </div>
            <div className="flex flex-col items-end gap-2">
              <div className="flex items-center gap-2">
                <div className="flex items-center h-8 rounded-md border border-input bg-background overflow-hidden">
                  <input
                    type="number"
                    min={1}
                    max={10}
                    step={1}
                    value={enrichLimit}
                    onChange={(e) => {
                      const n = Number(e.target.value);
                      // Clamp: 1..10. Anything else snaps to nearest valid.
                      if (Number.isFinite(n)) {
                        setEnrichLimit(Math.max(1, Math.min(10, Math.round(n))));
                      }
                    }}
                    disabled={enrichBusy}
                    aria-label="Number of leads to enrich (1-10)"
                    title="Max leads per click (1-10)"
                    className="h-full w-10 px-1 text-xs text-center bg-transparent outline-none border-r border-input"
                  />
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={enrichBusy}
                    onClick={runEnrichment}
                    className="h-full rounded-none"
                    title={
                      campaign.platform === "discord"
                        ? "Hit Discord's public invite API for the top N unvalidated leads and persist the verified server name"
                        : campaign.platform === "slack"
                          ? "Fetch each Slack invite's public landing page for the top N unvalidated leads. Tokens auto-expire ~30 days, so a high dead rate is normal."
                          : "Hit WhatsApp's invite landing page for the top N unvalidated leads and persist the verified group name"
                    }
                  >
                    {enrichBusy
                      ? "Validating…"
                      : campaign.platform === "discord"
                        ? "Validate via Discord"
                        : campaign.platform === "slack"
                          ? "Validate Slack invites"
                          : "Enrich WhatsApp"}
                  </Button>
                </div>
                <div className="flex items-center h-8 rounded-md border border-input bg-background overflow-hidden">
                  <input
                    type="number"
                    min={1}
                    max={10}
                    step={1}
                    value={autoTagLimit}
                    onChange={(e) => {
                      const n = Number(e.target.value);
                      if (Number.isFinite(n)) {
                        setAutoTagLimit(Math.max(1, Math.min(10, Math.round(n))));
                      }
                    }}
                    disabled={autoTagBusy}
                    aria-label="Number of leads to auto-tag (1-10)"
                    title="Max leads per click (1-10)"
                    className="h-full w-10 px-1 text-xs text-center bg-transparent outline-none border-r border-input"
                  />
                  <Button
                    size="sm"
                    variant="ghost"
                    disabled={autoTagBusy}
                    onClick={runAutoTag}
                    className="h-full rounded-none"
                    title="LLM-tag the top N untagged leads (highest score first)"
                  >
                    {autoTagBusy ? "Tagging…" : "Run auto-tag"}
                  </Button>
                </div>
                <Button
                  size="sm"
                  variant="destructive"
                  disabled={deleteBusy}
                  onClick={deleteDeadLeads}
                  className="h-8"
                  title="Permanently delete leads whose invite has been verified dead"
                >
                  {deleteBusy ? "Deleting…" : "Delete dead"}
                </Button>
                <ExportPanel campaignId={cid} campaignName={campaign.name} tags={tags} />
              </div>
              {enrichMsg && (
                <span className="text-[10px] max-w-[260px] text-right text-muted-foreground">
                  {enrichMsg}
                </span>
              )}
              {autoTagMsg && (
                <span className="text-[10px] text-muted-foreground max-w-[260px] text-right">
                  {autoTagMsg}
                </span>
              )}
              {deleteMsg && (
                <span className="text-[10px] text-muted-foreground max-w-[260px] text-right">
                  {deleteMsg}
                </span>
              )}
            </div>
          </CardHeader>
          <CardContent className="p-0">
            <div className="flex flex-wrap gap-1 border-b px-4 py-2">
              {(["all", ...LEAD_STATUSES.filter((s) => s !== "pending")] as const).map((s) => {
                const n =
                  s === "all"
                    ? counts
                      ? Object.values(counts).reduce((a, b) => a + b, 0)
                      : 0
                    : (counts?.[s] ?? 0);
                const active = statusFilter === s;
                return (
                  <button
                    key={s}
                    onClick={() => setStatusFilter(s)}
                    className={
                      "rounded-md px-2.5 py-1 text-xs font-medium capitalize transition-colors " +
                      (active
                        ? "bg-primary text-primary-foreground"
                        : "text-muted-foreground hover:bg-muted")
                    }
                  >
                    {s}
                    <span
                      className={
                        "ml-1.5 rounded px-1 tabular-nums text-[10px] " +
                        (active ? "bg-primary-foreground/20" : "bg-muted")
                      }
                    >
                      {n}
                    </span>
                  </button>
                );
              })}
            </div>
            {tags && tags.length > 0 && (
              <div className="flex flex-wrap items-center gap-1 border-b px-4 py-2">
                <span className="text-[10px] uppercase tracking-wider text-muted-foreground mr-1">
                  Tags
                </span>
                {(() => {
                  // Pin order: industry → business → misc → everything else
                  // (alphabetical). Industry/business/misc are auto-managed
                  // and represent mutually exclusive buckets of all live leads.
                  const industryNames = new Set(
                    campaign.industries.map((i) => i.toLowerCase().trim()),
                  );
                  const rank = (name: string) => {
                    if (industryNames.has(name)) return 0;
                    if (name === "business") return 1;
                    if (name === "misc") return 2;
                    return 3;
                  };
                  const ordered = [...tags].sort((a, b) => {
                    const ra = rank(a.name);
                    const rb = rank(b.name);
                    if (ra !== rb) return ra - rb;
                    return a.name.localeCompare(b.name);
                  });
                  return ordered.map((t) => {
                    const active = tagFilter.includes(t.id);
                    const r = rank(t.name);
                    const isIndustry = r === 0;
                    const isBusiness = r === 1;
                    const isMisc = r === 2;
                    const styleClass = active
                      ? "border-primary bg-primary text-primary-foreground"
                      : isIndustry
                      ? "border-emerald-500/60 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 hover:bg-emerald-500/20"
                      : isBusiness
                      ? "border-amber-500/60 bg-amber-500/10 text-amber-700 dark:text-amber-300 hover:bg-amber-500/20"
                      : isMisc
                      ? "border-dashed border-border bg-background text-muted-foreground hover:bg-muted"
                      : "border-border bg-background text-muted-foreground hover:bg-muted";
                    const title = isIndustry
                      ? `Industry tag · ${t.leads_count ?? 0} leads`
                      : isBusiness
                      ? `Business-adjacent (no industry tag) · ${t.leads_count ?? 0}`
                      : isMisc
                      ? `Neither industry nor business · ${t.leads_count ?? 0}`
                      : `Filter by ${t.name} (${t.leads_count ?? 0})`;
                    return (
                      <button
                        key={t.id}
                        onClick={() => toggleTagFilter(t.id)}
                        className={
                          "rounded-full border px-2 py-0.5 text-[11px] font-medium transition-colors " +
                          styleClass
                        }
                        title={title}
                      >
                        {t.name}
                        <span className="ml-1 tabular-nums opacity-70">
                          {t.leads_count ?? 0}
                        </span>
                      </button>
                    );
                  });
                })()}
                {tagFilter.length > 0 && (
                  <button
                    onClick={() => setTagFilter([])}
                    className="ml-1 text-[10px] text-muted-foreground hover:underline"
                  >
                    clear
                  </button>
                )}
              </div>
            )}
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Invite</TableHead>
                  <TableHead>Tags</TableHead>
                  <TableHead className="text-right">Total</TableHead>
                  <TableHead className="text-right">Rel</TableHead>
                  <TableHead className="text-right">Geo</TableHead>
                  <TableHead className="text-right">Eng</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {leads && leads.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={7} className="py-6 text-center text-muted-foreground">
                      No {statusFilter === "all" ? "" : `${statusFilter} `}leads{tagFilter.length > 0 ? " for the chosen tags" : ""}.
                    </TableCell>
                  </TableRow>
                )}
                {leads?.map((l) => (
                  <TableRow key={l.id}>
                    <TableCell>
                      <div className="flex items-center gap-1.5">
                        <a
                          href={inviteUrl(campaign.platform, l.invite_id)}
                          target="_blank"
                          rel="noreferrer"
                          className="inline-flex items-center gap-1 hover:underline text-sm font-medium max-w-[240px] truncate"
                          title={l.verified_group_name || l.invite_id}
                        >
                          {l.verified_group_name || (
                            <span className="font-mono text-xs">
                              {l.invite_id.slice(0, 18)}
                              {l.invite_id.length > 18 && "…"}
                            </span>
                          )}
                          <ExternalLink className="h-3 w-3 shrink-0" />
                        </a>
                        {l.last_validated_at && !l.verified_group_name && (
                          <span
                            className="rounded bg-amber-500/15 text-amber-700 dark:text-amber-300 px-1 py-0.5 text-[10px] font-medium uppercase tracking-wider"
                            title="Invite link is dead — verified by enrichment"
                          >
                            dead
                          </span>
                        )}
                      </div>
                      {l.source_url && (
                        <div className="truncate text-[10px] text-muted-foreground max-w-[240px]">
                          via {new URL(l.source_url).hostname}
                        </div>
                      )}
                    </TableCell>
                    <TableCell>
                      <div className="flex flex-wrap gap-1">
                        {l.tags.length === 0 ? (
                          <span className="text-[10px] text-muted-foreground">—</span>
                        ) : (
                          l.tags.map((name) => (
                            <span
                              key={name}
                              className="rounded border bg-muted px-1.5 py-0.5 text-[10px] font-medium text-muted-foreground"
                            >
                              {name}
                            </span>
                          ))
                        )}
                      </div>
                    </TableCell>
                    <TableCell className="text-right tabular-nums font-semibold">
                      {l.total_score ?? "—"}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {l.relevance ?? "—"}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {l.geo_fit ?? "—"}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {l.engagement ?? "—"}
                    </TableCell>
                    <TableCell className="text-right">
                      <LeadActions
                        lead={l}
                        busy={pendingLeadId === l.id}
                        onChange={(next) => setStatus(l.id, next)}
                      />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
