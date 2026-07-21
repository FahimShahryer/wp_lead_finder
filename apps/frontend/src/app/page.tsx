"use client";

import Link from "next/link";
import useSWR from "swr";
import {
  ArrowRight,
  RefreshCw,
  Plus,
  Megaphone,
  Sparkles,
  Target,
  Users,
  Coins,
} from "lucide-react";

import { CampaignSummary, fetcher } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { StatCard } from "@/components/stat-card";
import { StatusBadge } from "@/components/status-badge";
import { PlatformBadge } from "@/components/platform-badge";
import { NumbersCard } from "@/components/numbers-card";

function timeAgo(iso: string): string {
  const s = Math.max(1, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.round(h / 24);
  return `${d}d ago`;
}

export default function DashboardPage() {
  const { data, isLoading, error, mutate, isValidating } = useSWR<CampaignSummary[]>(
    "/campaigns",
    fetcher,
    { refreshInterval: 3000 },
  );

  const totals = (data ?? []).reduce(
    (a, c) => {
      a.leads += c.leads_count;
      a.scored += c.scored_leads_count;
      a.credits += c.serper_credits_used + c.firecrawl_credits_used;
      if (c.status === "running") a.running += 1;
      return a;
    },
    { leads: 0, scored: 0, credits: 0, running: 0 },
  );

  return (
    <div className="space-y-7">
      {/* Page header */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-2xl font-bold tracking-tight sm:text-3xl">Campaigns</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            Find, score and triage community leads across WhatsApp, Discord &amp; Slack.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => mutate()} disabled={isValidating}>
            <RefreshCw className={"h-4 w-4 " + (isValidating ? "animate-spin" : "")} />
            Refresh
          </Button>
          <Link href="/campaigns/new">
            <Button size="sm">
              <Plus className="h-4 w-4" /> New campaign
            </Button>
          </Link>
        </div>
      </div>

      {/* KPI row */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {isLoading ? (
          Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-[92px] rounded-xl" />)
        ) : (
          <>
            <StatCard label="Campaigns" value={data?.length ?? 0} icon={<Target className="h-4 w-4" />}
              hint={`${totals.running} running now`} accent="primary" />
            <StatCard label="Total leads" value={totals.leads.toLocaleString()} icon={<Users className="h-4 w-4" />}
              hint="across all runs" accent="sky" />
            <StatCard label="Scored leads" value={totals.scored.toLocaleString()} icon={<Sparkles className="h-4 w-4" />}
              hint={totals.leads ? `${Math.round((totals.scored / totals.leads) * 100)}% of leads` : "—"} accent="violet" />
            <StatCard label="Credits used" value={totals.credits.toLocaleString()} icon={<Coins className="h-4 w-4" />}
              hint="Serper + Firecrawl" accent="amber" />
          </>
        )}
      </div>

      <NumbersCard />

      {error && (
        <div className="rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">
          Failed to load campaigns: {String(error)}
        </div>
      )}

      {/* Campaigns table card */}
      <div className="overflow-hidden rounded-xl border border-border bg-card shadow-card">
        <div className="flex items-center justify-between border-b border-border px-5 py-3.5">
          <div className="flex items-center gap-2 text-sm font-semibold">
            <Megaphone className="h-4 w-4 text-primary" />
            All runs
            <span className="rounded-full bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground tabular-nums">
              {data?.length ?? 0}
            </span>
          </div>
          <span className="text-xs text-muted-foreground">Auto-refreshes every 3s</span>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border text-left text-xs uppercase tracking-wider text-muted-foreground">
                <th className="px-5 py-2.5 font-medium">Campaign</th>
                <th className="px-3 py-2.5 font-medium">Platform</th>
                <th className="px-3 py-2.5 font-medium">Status</th>
                <th className="px-3 py-2.5 text-right font-medium">Leads</th>
                <th className="px-3 py-2.5 text-right font-medium">Scored</th>
                <th className="hidden px-3 py-2.5 text-right font-medium md:table-cell">Serper</th>
                <th className="hidden px-3 py-2.5 text-right font-medium md:table-cell">Firecrawl</th>
                <th className="hidden px-3 py-2.5 font-medium lg:table-cell">Created</th>
                <th className="px-3 py-2.5" />
              </tr>
            </thead>
            <tbody>
              {isLoading &&
                Array.from({ length: 5 }).map((_, i) => (
                  <tr key={i} className="border-b border-border/60">
                    <td className="px-5 py-3.5"><Skeleton className="h-4 w-48" /></td>
                    <td className="px-3 py-3.5"><Skeleton className="h-5 w-16 rounded-full" /></td>
                    <td className="px-3 py-3.5"><Skeleton className="h-5 w-20 rounded-full" /></td>
                    <td className="px-3 py-3.5"><Skeleton className="ml-auto h-4 w-8" /></td>
                    <td className="px-3 py-3.5"><Skeleton className="ml-auto h-4 w-8" /></td>
                    <td className="hidden px-3 py-3.5 md:table-cell"><Skeleton className="ml-auto h-4 w-8" /></td>
                    <td className="hidden px-3 py-3.5 md:table-cell"><Skeleton className="ml-auto h-4 w-8" /></td>
                    <td className="hidden px-3 py-3.5 lg:table-cell"><Skeleton className="h-4 w-24" /></td>
                    <td className="px-3 py-3.5" />
                  </tr>
                ))}

              {data && data.length === 0 && (
                <tr>
                  <td colSpan={9} className="px-5 py-16 text-center">
                    <div className="mx-auto flex max-w-sm flex-col items-center gap-3">
                      <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-primary/10 text-primary">
                        <Target className="h-6 w-6" />
                      </span>
                      <div className="text-base font-semibold">No campaigns yet</div>
                      <p className="text-sm text-muted-foreground">
                        Spin up your first campaign to start finding community leads.
                      </p>
                      <Link href="/campaigns/new">
                        <Button size="sm" className="mt-1">
                          <Plus className="h-4 w-4" /> Create campaign
                        </Button>
                      </Link>
                    </div>
                  </td>
                </tr>
              )}

              {data?.map((c) => (
                <tr
                  key={c.id}
                  className="group border-b border-border/60 transition-colors last:border-0 hover:bg-muted/40"
                >
                  <td className="px-5 py-3">
                    <Link href={`/campaigns/${c.id}`} className="flex flex-col">
                      <span className="font-medium leading-tight group-hover:text-primary">{c.name}</span>
                      <span className="text-xs text-muted-foreground">#{c.id}</span>
                    </Link>
                  </td>
                  <td className="px-3 py-3"><PlatformBadge platform={c.platform} /></td>
                  <td className="px-3 py-3"><StatusBadge status={c.status} stage={c.current_stage} /></td>
                  <td className="px-3 py-3 text-right font-semibold tabular-nums">{c.leads_count}</td>
                  <td className="px-3 py-3 text-right tabular-nums text-muted-foreground">{c.scored_leads_count}</td>
                  <td className="hidden px-3 py-3 text-right tabular-nums text-muted-foreground md:table-cell">{c.serper_credits_used}</td>
                  <td className="hidden px-3 py-3 text-right tabular-nums text-muted-foreground md:table-cell">{c.firecrawl_credits_used}</td>
                  <td className="hidden px-3 py-3 text-xs text-muted-foreground lg:table-cell" title={new Date(c.created_at).toLocaleString()}>
                    {timeAgo(c.created_at)}
                  </td>
                  <td className="px-3 py-3 text-right">
                    <Link
                      href={`/campaigns/${c.id}`}
                      className="inline-flex h-8 w-8 items-center justify-center rounded-lg text-muted-foreground opacity-0 transition-all group-hover:opacity-100 hover:bg-primary/10 hover:text-primary"
                      aria-label="Open campaign"
                    >
                      <ArrowRight className="h-4 w-4" />
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
