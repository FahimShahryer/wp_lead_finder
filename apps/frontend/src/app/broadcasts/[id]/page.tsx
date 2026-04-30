"use client";

import Link from "next/link";
import { use, useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowLeft,
  CheckCircle2,
  Loader2,
  Megaphone,
} from "lucide-react";

import { BroadcastDetail, BroadcastStatus, api } from "@/lib/api";

const POLL_MS = 1500;
const TERMINAL = new Set<BroadcastStatus>(["completed", "failed", "cancelled"]);

const STATUS_BG: Record<BroadcastStatus, string> = {
  queued: "bg-muted text-muted-foreground",
  running: "bg-blue-500/15 text-blue-700 dark:text-blue-300",
  completed: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  failed: "bg-destructive/15 text-destructive",
  cancelled: "bg-muted text-muted-foreground",
};

export default function BroadcastDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = use(params);
  const jobId = Number(id);

  const [detail, setDetail] = useState<BroadcastDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    async function tick() {
      try {
        const d = await api.getBroadcast(jobId);
        setDetail(d);
        if (TERMINAL.has(d.status) && timer.current) {
          clearInterval(timer.current);
          timer.current = null;
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    }
    tick();
    timer.current = setInterval(tick, POLL_MS);
    return () => {
      if (timer.current) clearInterval(timer.current);
      timer.current = null;
    };
  }, [jobId]);

  if (error && !detail) {
    return (
      <div className="rounded-md border border-destructive/40 bg-destructive/5 p-4 text-sm text-destructive">
        {error}
      </div>
    );
  }
  if (!detail) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground p-8">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading broadcast {jobId}…
      </div>
    );
  }

  const done = detail.sent_count + detail.failed_count;
  const pct = detail.total_targets ? Math.round((done / detail.total_targets) * 100) : 0;

  return (
    <div className="space-y-4">
      <div>
        <Link
          href="/broadcasts"
          className="text-xs text-muted-foreground hover:underline"
        >
          <ArrowLeft className="mr-1 inline h-3 w-3" />
          All broadcasts
        </Link>
        <h1 className="mt-1 flex items-center gap-2 text-2xl font-semibold tracking-tight">
          <Megaphone className="h-5 w-5" />
          Broadcast #{jobId}
        </h1>
      </div>

      {/* Summary card */}
      <div className="rounded-md border bg-card p-4 space-y-3">
        <div className="flex items-center justify-between gap-3">
          <span
            className={
              "inline-flex items-center gap-1.5 rounded px-2.5 py-1 text-xs font-bold uppercase tracking-wider " +
              STATUS_BG[detail.status]
            }
          >
            {detail.status === "running" || detail.status === "queued" ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : detail.status === "completed" ? (
              <CheckCircle2 className="h-3.5 w-3.5" />
            ) : (
              <AlertTriangle className="h-3.5 w-3.5" />
            )}
            {detail.status}
          </span>
          <div className="text-xs text-muted-foreground">
            {done}/{detail.total_targets} processed · {detail.sent_count} sent ·{" "}
            <span className={detail.failed_count > 0 ? "text-destructive" : ""}>
              {detail.failed_count} failed
            </span>
          </div>
        </div>
        <div className="h-1.5 w-full rounded-full bg-muted overflow-hidden">
          <div
            className={
              "h-full transition-all " +
              (detail.status === "failed" ? "bg-destructive" : "bg-primary")
            }
            style={{ width: `${pct}%` }}
          />
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
          <Field label="Throttle" value={`${detail.min_delay_seconds}–${detail.max_delay_seconds}s`} />
          <Field
            label="Created"
            value={new Date(detail.created_at).toLocaleString()}
          />
          <Field
            label="Started"
            value={detail.started_at ? new Date(detail.started_at).toLocaleString() : "—"}
          />
          <Field
            label="Completed"
            value={
              detail.completed_at
                ? new Date(detail.completed_at).toLocaleString()
                : "—"
            }
          />
        </div>
        {detail.error && (
          <div className="rounded-md border border-destructive/40 bg-destructive/5 p-2 text-xs text-destructive">
            {detail.error}
          </div>
        )}
      </div>

      {/* Body */}
      <div className="rounded-md border bg-card p-4 space-y-2">
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
          Message body
        </div>
        <div className="text-sm whitespace-pre-wrap break-words">{detail.body}</div>
      </div>

      {/* Per-message log */}
      <div className="rounded-md border bg-card overflow-hidden">
        <div className="border-b px-4 py-2 text-[10px] uppercase tracking-wider text-muted-foreground">
          Per-message log ({detail.messages.length})
        </div>
        <div className="max-h-[60vh] overflow-y-auto">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-muted">
              <tr className="text-left">
                <th className="px-3 py-2 font-medium">Group</th>
                <th className="px-3 py-2 font-medium w-24">Status</th>
                <th className="px-3 py-2 font-medium">Note</th>
                <th className="px-3 py-2 font-medium w-32">Attempted</th>
              </tr>
            </thead>
            <tbody>
              {detail.messages.map((m) => (
                <tr key={m.id} className="border-t">
                  <td className="px-3 py-2 truncate max-w-[300px]">
                    {m.group_subject || (
                      <span className="font-mono text-[10px]">{m.jid}</span>
                    )}
                  </td>
                  <td className="px-3 py-2">
                    <span
                      className={
                        "rounded px-1.5 py-0.5 text-[10px] font-medium " +
                        (m.status === "sent"
                          ? "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300"
                          : m.status === "failed"
                          ? "bg-destructive/15 text-destructive"
                          : "bg-muted text-muted-foreground")
                      }
                    >
                      {m.status}
                    </span>
                  </td>
                  <td className="px-3 py-2 text-muted-foreground truncate max-w-[260px]">
                    {m.error || ""}
                  </td>
                  <td className="px-3 py-2 text-muted-foreground tabular-nums">
                    {new Date(m.attempted_at).toLocaleTimeString()}
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

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {label}
      </div>
      <div className="text-foreground tabular-nums">{value}</div>
    </div>
  );
}
