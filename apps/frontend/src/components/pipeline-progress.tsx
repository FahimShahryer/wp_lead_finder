"use client";

import {
  Sparkles,
  Search,
  Filter,
  Users,
  MessagesSquare,
  Globe,
  Link2,
  Brain,
  Check,
  Loader2,
  CheckCircle2,
  AlertTriangle,
  XCircle,
} from "lucide-react";
import { cn } from "@/lib/utils";

// Mirrors the backend WhatsApp orchestrator's stage order exactly.
// (queries → search → prefilter → seed_reddit → fetch_reddit → fetch_web → extract → score)
const STAGES = [
  { key: "queries", label: "Queries", caption: "Asking the AI for targeted search queries", icon: Sparkles },
  { key: "search", label: "Search", caption: "Running those queries across the web", icon: Search },
  { key: "prefilter", label: "Filter", caption: "Deciding which pages are worth reading", icon: Filter },
  { key: "seed_reddit", label: "Communities", caption: "Mining relevant Reddit communities", icon: Users },
  { key: "fetch_reddit", label: "Reddit", caption: "Reading Reddit threads & comments", icon: MessagesSquare },
  { key: "fetch_web", label: "Scrape", caption: "Scraping web pages for invite links", icon: Globe },
  { key: "extract", label: "Extract", caption: "Pulling out WhatsApp group invites", icon: Link2 },
  { key: "score", label: "Score", caption: "Ranking each lead with AI", icon: Brain },
] as const;

type Campaign = {
  status: string;
  current_stage: string | null;
  queries_count: number;
  search_results_count: number;
  leads_count: number;
  scored_leads_count: number;
  serper_credits_used: number;
  max_credits_serper: number;
  firecrawl_credits_used: number;
  max_credits_firecrawl: number;
};

function Metric({ label, value, sub, active }: { label: string; value: React.ReactNode; sub?: string; active?: boolean }) {
  return (
    <div
      className={cn(
        "rounded-lg border px-3 py-2 transition-colors",
        active ? "border-primary/40 bg-primary/5" : "border-border bg-background/40",
      )}
    >
      <div className="text-[10px] font-medium uppercase tracking-wider text-muted-foreground">{label}</div>
      <div className="mt-0.5 text-lg font-bold tabular-nums leading-none">{value}</div>
      {sub && <div className="mt-0.5 text-[10px] text-muted-foreground">{sub}</div>}
    </div>
  );
}

export function PipelineProgress({ campaign }: { campaign: Campaign }) {
  const running = campaign.status === "running";
  const stageIndex = Math.max(0, STAGES.findIndex((s) => s.key === campaign.current_stage));
  const activeKey = running ? STAGES[stageIndex]?.key : null;
  const doneThrough = running ? stageIndex : STAGES.length; // steps before current are done
  const pct = running ? Math.round(((stageIndex + 0.5) / STAGES.length) * 100) : 100;

  const banner =
    campaign.status === "done"
      ? { icon: <CheckCircle2 className="h-5 w-5" />, tone: "text-success", ring: "border-success/30 bg-success/5", title: "Campaign complete", sub: "All stages finished — your leads are ready below." }
      : campaign.status === "budget_exceeded"
        ? { icon: <AlertTriangle className="h-5 w-5" />, tone: "text-warning", ring: "border-warning/30 bg-warning/5", title: "Finished — budget reached", sub: "A paid step hit its credit cap; leads from what was fetched are below." }
        : campaign.status === "failed"
          ? { icon: <XCircle className="h-5 w-5" />, tone: "text-destructive", ring: "border-destructive/30 bg-destructive/5", title: "Run failed", sub: "Something went wrong mid-run. Check the logs and re-run." }
          : { icon: <Loader2 className="h-5 w-5 animate-spin" />, tone: "text-primary", ring: "border-primary/30 bg-primary/5", title: STAGES[stageIndex]?.caption ?? "Working…", sub: `Step ${stageIndex + 1} of ${STAGES.length} — sit tight, this usually takes 1–2 minutes.` };

  return (
    <div className="overflow-hidden rounded-xl border border-border bg-card shadow-card">
      {/* Banner */}
      <div className={cn("flex items-start gap-3 border-b px-5 py-4", banner.ring)}>
        <span className={cn("mt-0.5 shrink-0", banner.tone)}>{banner.icon}</span>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-base font-bold tracking-tight">{banner.title}</h2>
            {running && (
              <span className="inline-flex items-center gap-1 rounded-full bg-primary/10 px-2 py-0.5 text-[11px] font-semibold text-primary">
                <span className="relative flex h-1.5 w-1.5">
                  <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-primary opacity-70" />
                  <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-primary" />
                </span>
                Live
              </span>
            )}
          </div>
          <p className="mt-0.5 text-sm text-muted-foreground">{banner.sub}</p>
        </div>
      </div>

      <div className="space-y-4 p-5">
        {/* Progress bar */}
        <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
          <div
            className={cn(
              "h-full rounded-full transition-all duration-700 ease-out",
              campaign.status === "failed" ? "bg-destructive" : campaign.status === "budget_exceeded" ? "bg-warning" : "bg-primary",
            )}
            style={{ width: `${pct}%` }}
          />
        </div>

        {/* Stepper */}
        <div className="flex items-start gap-1 overflow-x-auto pb-1">
          {STAGES.map((s, i) => {
            const done = i < doneThrough;
            const active = s.key === activeKey;
            const Icon = s.icon;
            return (
              <div key={s.key} className="flex min-w-0 flex-1 flex-col items-center gap-1.5 text-center">
                <div className="flex w-full items-center">
                  <span className={cn("h-0.5 flex-1 rounded", i === 0 ? "bg-transparent" : done || active ? "bg-primary" : "bg-border")} />
                  <span
                    className={cn(
                      "flex h-9 w-9 shrink-0 items-center justify-center rounded-full border-2 transition-all",
                      done
                        ? "border-primary bg-primary text-primary-foreground"
                        : active
                          ? "border-primary bg-primary/10 text-primary shadow-lift"
                          : "border-border bg-background text-muted-foreground",
                    )}
                  >
                    {done ? <Check className="h-4 w-4" /> : active ? <Icon className="h-4 w-4 animate-pulse" /> : <Icon className="h-4 w-4" />}
                  </span>
                  <span className={cn("h-0.5 flex-1 rounded", i === STAGES.length - 1 ? "bg-transparent" : done ? "bg-primary" : "bg-border")} />
                </div>
                {/* Labels collide when 8-across on phones; the banner already
                    names the active stage, so show labels from sm up only. */}
                <span className={cn("hidden text-[10px] font-medium leading-tight sm:block", active ? "text-primary" : done ? "text-foreground" : "text-muted-foreground")}>
                  {s.label}
                </span>
              </div>
            );
          })}
        </div>

        {/* Live metrics */}
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
          <Metric label="Queries" value={campaign.queries_count} active={activeKey === "queries"} />
          <Metric label="Results" value={campaign.search_results_count} active={activeKey === "search" || activeKey === "prefilter"} />
          <Metric label="Leads" value={campaign.leads_count} active={activeKey === "extract" || activeKey === "fetch_web" || activeKey === "fetch_reddit"} />
          <Metric label="Scored" value={campaign.scored_leads_count} sub={`of ${campaign.leads_count}`} active={activeKey === "score"} />
          <Metric label="Serper" value={`${campaign.serper_credits_used}/${campaign.max_credits_serper}`} active={activeKey === "search"} />
          <Metric label="Firecrawl" value={`${campaign.firecrawl_credits_used}/${campaign.max_credits_firecrawl}`} active={activeKey === "fetch_web"} />
        </div>
      </div>
    </div>
  );
}
