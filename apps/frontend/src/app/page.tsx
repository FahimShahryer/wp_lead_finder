"use client";

import Link from "next/link";
import useSWR from "swr";
import { ArrowRight, RefreshCw } from "lucide-react";

import { CampaignSummary, fetcher } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { StatusBadge } from "@/components/status-badge";
import { PlatformBadge } from "@/components/platform-badge";
import { NumbersCard } from "@/components/numbers-card";

export default function DashboardPage() {
  const { data, isLoading, error, mutate } = useSWR<CampaignSummary[]>("/campaigns", fetcher, {
    refreshInterval: 3000,
  });

  return (
    <div className="space-y-6">
      <NumbersCard />

      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Campaigns</h1>
          <p className="text-sm text-muted-foreground">
            All runs, sorted by most recent. Auto-refreshes every 3 seconds.
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" onClick={() => mutate()}>
            <RefreshCw className="mr-2 h-4 w-4" /> Refresh
          </Button>
          <Link href="/campaigns/new">
            <Button size="sm">+ New campaign</Button>
          </Link>
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">
          Failed to load: {String(error)}
        </div>
      )}

      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="w-[36%]">Name</TableHead>
              <TableHead>Platform</TableHead>
              <TableHead>Status</TableHead>
              <TableHead className="text-right">Leads</TableHead>
              <TableHead className="text-right">Scored</TableHead>
              <TableHead className="text-right">Serper</TableHead>
              <TableHead className="text-right">Firecrawl</TableHead>
              <TableHead>Created</TableHead>
              <TableHead className="w-12" />
            </TableRow>
          </TableHeader>
          <TableBody>
            {isLoading && (
              <TableRow>
                <TableCell colSpan={9} className="text-center text-muted-foreground">
                  Loading…
                </TableCell>
              </TableRow>
            )}
            {data && data.length === 0 && (
              <TableRow>
                <TableCell colSpan={9} className="text-center text-muted-foreground py-8">
                  No campaigns yet.{" "}
                  <Link href="/campaigns/new" className="underline">
                    Create your first one.
                  </Link>
                </TableCell>
              </TableRow>
            )}
            {data?.map((c) => (
              <TableRow key={c.id}>
                <TableCell className="font-medium">
                  <Link href={`/campaigns/${c.id}`} className="hover:underline">
                    {c.name}
                  </Link>
                  <div className="text-xs text-muted-foreground">#{c.id}</div>
                </TableCell>
                <TableCell>
                  <PlatformBadge platform={c.platform} />
                </TableCell>
                <TableCell>
                  <StatusBadge status={c.status} stage={c.current_stage} />
                </TableCell>
                <TableCell className="text-right tabular-nums">{c.leads_count}</TableCell>
                <TableCell className="text-right tabular-nums">
                  {c.scored_leads_count}
                </TableCell>
                <TableCell className="text-right tabular-nums">
                  {c.serper_credits_used}
                </TableCell>
                <TableCell className="text-right tabular-nums">
                  {c.firecrawl_credits_used}
                </TableCell>
                <TableCell className="text-sm text-muted-foreground">
                  {new Date(c.created_at).toLocaleString()}
                </TableCell>
                <TableCell>
                  <Link href={`/campaigns/${c.id}`}>
                    <Button variant="ghost" size="icon">
                      <ArrowRight className="h-4 w-4" />
                    </Button>
                  </Link>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
