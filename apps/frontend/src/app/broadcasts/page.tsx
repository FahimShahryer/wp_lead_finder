"use client";

import Link from "next/link";
import { useState } from "react";
import useSWR from "swr";
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  ChevronRight,
  Loader2,
  Megaphone,
} from "lucide-react";

import { BroadcastStatus, BroadcastSummary, fetcher } from "@/lib/api";

const TERMINAL = new Set<BroadcastStatus>(["completed", "failed", "cancelled"]);

const STATUS_VARIANT: Record<BroadcastStatus, string> = {
  queued: "bg-muted text-muted-foreground",
  running: "bg-blue-500/15 text-blue-700 dark:text-blue-300",
  completed: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  failed: "bg-destructive/15 text-destructive",
  cancelled: "bg-muted text-muted-foreground",
};

function StatusIcon({ s }: { s: BroadcastStatus }) {
  if (s === "running" || s === "queued") return <Loader2 className="h-3.5 w-3.5 animate-spin" />;
  if (s === "completed") return <CheckCircle2 className="h-3.5 w-3.5" />;
  return <AlertTriangle className="h-3.5 w-3.5" />;
}

function relativeTime(iso: string | null): string {
  if (!iso) return "";
  const sec = Math.max(1, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (sec < 60) return `${sec}s ago`;
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.round(hr / 24);
  if (day < 7) return `${day}d ago`;
  return new Date(iso).toLocaleDateString();
}

const FILTERS: { key: BroadcastStatus | "all"; label: string }[] = [
  { key: "all", label: "All" },
  { key: "running", label: "Running" },
  { key: "queued", label: "Queued" },
  { key: "completed", label: "Completed" },
  { key: "failed", label: "Failed" },
];

export default function BroadcastsPage() {
  const [filter, setFilter] = useState<BroadcastStatus | "all">("all");

  const params = new URLSearchParams({ limit: "100" });
  if (filter !== "all") params.set("status", filter);
  const key = `/broadcasts?${params.toString()}`;

  const { data: broadcasts } = useSWR<BroadcastSummary[]>(key, fetcher, {
    // Cheap poll while there's anything in flight; back off when everything is terminal.
    refreshInterval: (latest) => {
      if (!latest) return 4000;
      return latest.some((b) => !TERMINAL.has(b.status)) ? 2500 : 10000;
    },
  });

  return (
    <div className="space-y-4">
      <div>
        <Link href="/" className="text-xs text-muted-foreground hover:underline">
          <ArrowLeft className="mr-1 inline h-3 w-3" />
          Dashboard
        </Link>
        <h1 className="mt-1 flex items-center gap-2 text-2xl font-semibold tracking-tight">
          <Megaphone className="h-5 w-5" />
          Broadcasts
        </h1>
        <p className="text-sm text-muted-foreground">
          Every bulk-send across all your linked numbers, newest first. Click any row to inspect per-message status.
        </p>
      </div>

      <div className="flex flex-wrap gap-1">
        {FILTERS.map((f) => {
          const sel = filter === f.key;
          return (
            <button
              key={f.key}
              onClick={() => setFilter(f.key)}
              className={
                "rounded-full px-3 py-1 text-xs font-medium transition-colors " +
                (sel
                  ? "bg-primary text-primary-foreground"
                  : "bg-muted text-muted-foreground hover:bg-muted/80")
              }
            >
              {f.label}
            </button>
          );
        })}
      </div>

      <div className="rounded-md border bg-card overflow-hidden">
        {!broadcasts && (
          <div className="p-6 text-sm text-muted-foreground flex items-center gap-2">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading…
          </div>
        )}
        {broadcasts && broadcasts.length === 0 && (
          <div className="p-8 text-sm text-muted-foreground text-center">
            No broadcasts {filter !== "all" ? `with status ${filter}` : "yet"}.
          </div>
        )}
        {broadcasts && broadcasts.length > 0 && (
          <ul className="divide-y">
            {broadcasts.map((b) => {
              const done = b.sent_count + b.failed_count;
              const pct = b.total_targets ? Math.round((done / b.total_targets) * 100) : 0;
              const isTerminal = TERMINAL.has(b.status);
              return (
                <li key={b.id}>
                  <Link
                    href={`/broadcasts/${b.id}`}
                    className="flex items-start gap-3 px-4 py-3 hover:bg-muted/40 transition-colors"
                  >
                    <div className="mt-1 shrink-0">
                      <span
                        className={
                          "inline-flex items-center gap-1 rounded px-2 py-0.5 text-[10px] font-bold uppercase tracking-wider " +
                          STATUS_VARIANT[b.status]
                        }
                      >
                        <StatusIcon s={b.status} />
                        {b.status}
                      </span>
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-baseline justify-between gap-2">
                        <div className="text-sm font-medium truncate">
                          {b.body.length > 80 ? b.body.slice(0, 80) + "…" : b.body}
                        </div>
                        <div className="text-[11px] text-muted-foreground shrink-0">
                          {relativeTime(b.created_at)}
                        </div>
                      </div>
                      <div className="text-[11px] text-muted-foreground mt-0.5">
                        via <span className="text-foreground">{b.number_display_name}</span>
                        {b.number_msisdn && (
                          <span className="font-mono"> · +{b.number_msisdn.replace(/\D/g, "")}</span>
                        )}
                        {" · "}
                        {b.sent_count}/{b.total_targets} sent
                        {b.failed_count > 0 && (
                          <span className="text-destructive">
                            {" · "}
                            {b.failed_count} failed
                          </span>
                        )}
                        {" · throttle "}
                        {b.min_delay_seconds}–{b.max_delay_seconds}s
                      </div>
                      {!isTerminal && b.total_targets > 0 && (
                        <div className="mt-1.5 h-1 w-full rounded-full bg-muted overflow-hidden">
                          <div
                            className="h-full bg-primary transition-all"
                            style={{ width: `${pct}%` }}
                          />
                        </div>
                      )}
                      {b.error && (
                        <div className="text-[11px] text-destructive mt-0.5 truncate">
                          {b.error}
                        </div>
                      )}
                    </div>
                    <ChevronRight className="h-4 w-4 text-muted-foreground self-center shrink-0" />
                  </Link>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
