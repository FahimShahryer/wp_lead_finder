"use client";

import Link from "next/link";
import { use } from "react";
import useSWR from "swr";
import { ArrowLeft, ExternalLink } from "lucide-react";

import { ActivityRow, CampaignStatus, Lead, fetcher } from "@/lib/api";
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

export default function CampaignPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const cid = Number(id);

  const { data: campaign } = useSWR<CampaignStatus>(`/campaigns/${cid}`, fetcher, {
    refreshInterval: (latest) =>
      latest && TERMINAL.has(latest.status) ? 0 : 2000,
  });

  const { data: activity } = useSWR<ActivityRow[]>(
    `/campaigns/${cid}/activity?limit=20`,
    fetcher,
    { refreshInterval: campaign && TERMINAL.has(campaign.status) ? 0 : 2000 },
  );

  const { data: leads } = useSWR<Lead[]>(
    `/campaigns/${cid}/leads?limit=100`,
    fetcher,
    { refreshInterval: campaign && TERMINAL.has(campaign.status) ? 0 : 5000 },
  );

  if (!campaign) return <div className="text-muted-foreground">Loading campaign…</div>;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <Link href="/" className="text-xs text-muted-foreground hover:underline">
            <ArrowLeft className="mr-1 inline h-3 w-3" />
            All campaigns
          </Link>
          <h1 className="mt-1 flex items-center gap-3 text-2xl font-semibold tracking-tight">
            {campaign.name}
            <span className="text-base font-normal text-muted-foreground">#{campaign.id}</span>
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
          <CardHeader>
            <CardTitle className="text-lg">Leads</CardTitle>
            <CardDescription>Ranked by total score (descending).</CardDescription>
          </CardHeader>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Invite</TableHead>
                  <TableHead className="text-right">Total</TableHead>
                  <TableHead className="text-right">Rel</TableHead>
                  <TableHead className="text-right">Geo</TableHead>
                  <TableHead className="text-right">Eng</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {leads && leads.length === 0 && (
                  <TableRow>
                    <TableCell colSpan={5} className="py-6 text-center text-muted-foreground">
                      No leads yet.
                    </TableCell>
                  </TableRow>
                )}
                {leads?.map((l) => (
                  <TableRow key={l.id}>
                    <TableCell className="font-mono text-xs">
                      <a
                        href={`https://chat.whatsapp.com/${l.invite_id}`}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex items-center gap-1 hover:underline"
                      >
                        {l.invite_id.slice(0, 18)}
                        {l.invite_id.length > 18 && "…"}
                        <ExternalLink className="h-3 w-3" />
                      </a>
                      {l.source_url && (
                        <div className="truncate text-[10px] text-muted-foreground max-w-[240px]">
                          via {new URL(l.source_url).hostname}
                        </div>
                      )}
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
