"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  Loader2,
  Search,
  Send,
  ShieldAlert,
  X,
} from "lucide-react";

import {
  BroadcastDetail,
  BroadcastJob,
  WhatsAppGroup,
  WhatsAppNumber,
  api,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

const POLL_MS = 1500;
const TERMINAL_BROADCAST = new Set(["completed", "failed", "cancelled"]);

type Step =
  | { kind: "compose" }
  | { kind: "running"; jobId: number };

export function BroadcastModal({
  number,
  onClose,
}: {
  number: WhatsAppNumber;
  onClose: () => void;
}) {
  const [step, setStep] = useState<Step>({ kind: "compose" });
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/60 backdrop-blur-sm p-4">
      <div className="w-full max-w-2xl rounded-lg border bg-card text-card-foreground shadow-lg max-h-[90vh] flex flex-col">
        <div className="flex items-center justify-between border-b px-4 py-3 shrink-0">
          <div>
            <h2 className="text-base font-semibold">Broadcast</h2>
            <p className="text-xs text-muted-foreground">
              from <span className="font-medium">{number.display_name}</span>
              {number.msisdn ? ` · +${number.msisdn.replace(/\D/g, "")}` : ""}
            </p>
          </div>
          <button
            onClick={onClose}
            aria-label="Close"
            className="text-muted-foreground hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="overflow-y-auto p-4">
          {step.kind === "compose" ? (
            <ComposeStep
              number={number}
              onStarted={(jobId) => setStep({ kind: "running", jobId })}
            />
          ) : (
            <RunningStep jobId={step.jobId} onClose={onClose} />
          )}
        </div>
      </div>
    </div>
  );
}

// ---------- Compose ----------

function ComposeStep({
  number,
  onStarted,
}: {
  number: WhatsAppNumber;
  onStarted: (jobId: number) => void;
}) {
  const [groups, setGroups] = useState<WhatsAppGroup[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [body, setBody] = useState("");
  const [minDelay, setMinDelay] = useState(3);
  const [maxDelay, setMaxDelay] = useState(15);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const gs = await api.listNumberGroups(number.id);
        if (!cancelled) setGroups(gs);
      } catch (e) {
        if (!cancelled) setLoadError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [number.id]);

  const filtered = useMemo(() => {
    if (!groups) return [];
    const q = search.trim().toLowerCase();
    if (!q) return groups;
    return groups.filter((g) => g.subject.toLowerCase().includes(q));
  }, [groups, search]);

  function toggle(jid: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(jid)) next.delete(jid);
      else next.add(jid);
      return next;
    });
  }

  function selectAll(visible: WhatsAppGroup[]) {
    setSelected((prev) => {
      const next = new Set(prev);
      for (const g of visible) {
        // Don't auto-select admin-only groups — sending will fail unless we're admin.
        if (!g.announce) next.add(g.jid);
      }
      return next;
    });
  }

  async function start() {
    if (selected.size === 0 || !body.trim()) return;
    if (maxDelay < minDelay) {
      setError("Max delay must be ≥ min delay.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const job = await api.createBroadcast(number.id, {
        body: body.trim(),
        targets: Array.from(selected),
        min_delay_seconds: minDelay,
        max_delay_seconds: maxDelay,
      });
      onStarted(job.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  if (number.status !== "connected") {
    return (
      <div className="rounded-md border border-amber-500/40 bg-amber-500/5 p-4 text-sm">
        <div className="flex items-start gap-2">
          <AlertTriangle className="h-4 w-4 mt-0.5 text-amber-700 dark:text-amber-300 shrink-0" />
          <div>
            <div className="font-medium">Number not connected</div>
            <div className="text-muted-foreground mt-1">
              Status is <span className="font-mono">{number.status}</span>. Reconnect the
              primary phone before broadcasting.
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Group picker */}
      <div className="space-y-2">
        <div className="flex items-center justify-between">
          <Label>Groups ({selected.size} selected)</Label>
          {filtered.length > 0 && (
            <div className="flex gap-2 text-xs">
              <button
                onClick={() => selectAll(filtered)}
                className="text-muted-foreground hover:underline"
              >
                Select all{search ? " visible" : ""}
              </button>
              {selected.size > 0 && (
                <button
                  onClick={() => setSelected(new Set())}
                  className="text-muted-foreground hover:underline"
                >
                  Clear
                </button>
              )}
            </div>
          )}
        </div>

        <div className="relative">
          <Search className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
          <Input
            placeholder="Search groups…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-8"
            disabled={!groups}
          />
        </div>

        <div className="max-h-64 overflow-y-auto rounded-md border bg-background">
          {loadError && (
            <div className="p-3 text-xs text-destructive">{loadError}</div>
          )}
          {!groups && !loadError && (
            <div className="p-3 text-xs text-muted-foreground flex items-center gap-2">
              <Loader2 className="h-3 w-3 animate-spin" /> Loading groups from primary phone…
            </div>
          )}
          {groups && groups.length === 0 && (
            <div className="p-3 text-xs text-muted-foreground">
              This number isn't a member of any groups yet. Join groups from your phone first.
            </div>
          )}
          {filtered.map((g) => {
            const checked = selected.has(g.jid);
            return (
              <label
                key={g.jid}
                className={
                  "flex items-center gap-2 px-3 py-2 border-b last:border-b-0 cursor-pointer hover:bg-muted/40 " +
                  (g.announce ? "opacity-60" : "")
                }
                title={g.announce ? "Admins-only — sending will fail unless you're an admin." : g.jid}
              >
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => toggle(g.jid)}
                  className="shrink-0"
                />
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-medium truncate">
                    {g.subject || "(no name)"}
                    {g.announce && (
                      <span className="ml-2 rounded bg-muted px-1 text-[10px] uppercase">
                        admins-only
                      </span>
                    )}
                  </div>
                  <div className="text-[10px] text-muted-foreground">
                    {g.participants_count} member{g.participants_count === 1 ? "" : "s"}
                  </div>
                </div>
              </label>
            );
          })}
        </div>
      </div>

      {/* Message body */}
      <div className="space-y-2">
        <Label htmlFor="body">Message</Label>
        <Textarea
          id="body"
          value={body}
          onChange={(e) => setBody(e.target.value)}
          rows={4}
          placeholder="Hi — this is …"
          className="resize-y"
        />
      </div>

      {/* Throttle */}
      <div className="grid grid-cols-2 gap-3">
        <div className="space-y-1">
          <Label htmlFor="min-d">Min delay (s)</Label>
          <Input
            id="min-d"
            type="number"
            min={0}
            max={600}
            value={minDelay}
            onChange={(e) => setMinDelay(Math.max(0, Number(e.target.value) || 0))}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="max-d">Max delay (s)</Label>
          <Input
            id="max-d"
            type="number"
            min={0}
            max={600}
            value={maxDelay}
            onChange={(e) => setMaxDelay(Math.max(0, Number(e.target.value) || 0))}
          />
        </div>
      </div>
      <p className="text-[11px] text-muted-foreground">
        Each send waits a random time between min and max. Default 3–15s feels human-paced;
        going lower raises ban risk.
      </p>

      {/* Ban-prevention guardrail — same content as Inbox reply box, lives
          right above the Start button so it's seen before sending. */}
      <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2.5 text-[11px] text-amber-900 dark:text-amber-200">
        <div className="flex items-start gap-2">
          <ShieldAlert className="h-3.5 w-3.5 shrink-0 mt-0.5" />
          <div className="space-y-1">
            <div className="font-semibold">Avoid getting banned</div>
            <ul className="list-disc list-inside space-y-0.5 marker:text-amber-700/60">
              <li>Don&apos;t blast identical text to many groups — vary it (rephrase, swap order).</li>
              <li>Keep delays at the default 3–15s or higher; lower = burst signature.</li>
              <li>Stay under ~80–100 outbound msgs / number / day until warmed up.</li>
              <li>Skip admins-only groups — they&apos;ll auto-fail and admins may report you.</li>
            </ul>
          </div>
        </div>
      </div>

      {error && (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
          {error}
        </div>
      )}

      <div className="flex items-center justify-end gap-2 border-t pt-3">
        <span className="mr-auto text-[11px] text-muted-foreground">
          {selected.size > 0 &&
            `Estimated runtime: ~${estimateMinutes(selected.size, minDelay, maxDelay)} min`}
        </span>
        <Button
          onClick={start}
          disabled={busy || selected.size === 0 || !body.trim()}
        >
          {busy ? (
            <>
              <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Starting…
            </>
          ) : (
            <>
              <Send className="mr-2 h-4 w-4" />
              Start broadcast ({selected.size})
            </>
          )}
        </Button>
      </div>
    </div>
  );
}

function estimateMinutes(n: number, minD: number, maxD: number): string {
  if (n <= 1) return "<1";
  const avg = (minD + maxD) / 2;
  const totalSeconds = (n - 1) * avg;
  const min = totalSeconds / 60;
  return min < 1 ? `~${Math.round(totalSeconds)}s` : min.toFixed(1);
}

// ---------- Running / progress ----------

function RunningStep({
  jobId,
  onClose,
}: {
  jobId: number;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<BroadcastDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    async function tick() {
      try {
        const d = await api.getBroadcast(jobId);
        setDetail(d);
        if (TERMINAL_BROADCAST.has(d.status) && timer.current) {
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
      <div className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
        {error}
      </div>
    );
  }
  if (!detail) {
    return (
      <div className="flex items-center justify-center py-12 text-sm text-muted-foreground">
        <Loader2 className="mr-2 h-4 w-4 animate-spin" /> Loading…
      </div>
    );
  }

  const done = detail.sent_count + detail.failed_count;
  const pct = detail.total_targets ? Math.round((done / detail.total_targets) * 100) : 0;
  const isTerminal = TERMINAL_BROADCAST.has(detail.status);

  return (
    <div className="space-y-4">
      <div className="rounded-md border bg-muted/30 p-3 space-y-2">
        <div className="flex items-center justify-between">
          <div className="text-sm">
            <span className="font-medium capitalize">{detail.status}</span>
            <span className="text-muted-foreground">
              {" "}— {done}/{detail.total_targets} processed · {detail.sent_count} sent ·{" "}
              {detail.failed_count} failed
            </span>
          </div>
          {detail.status === "running" && (
            <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
          )}
          {detail.status === "completed" && (
            <CheckCircle2 className="h-5 w-5 text-emerald-600" />
          )}
          {(detail.status === "failed" || detail.status === "cancelled") && (
            <AlertTriangle className="h-5 w-5 text-destructive" />
          )}
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
        {detail.error && (
          <div className="text-xs text-destructive">{detail.error}</div>
        )}
      </div>

      <div className="rounded-md border max-h-72 overflow-y-auto">
        <table className="w-full text-xs">
          <thead className="sticky top-0 bg-muted">
            <tr className="text-left">
              <th className="px-2 py-1.5 font-medium">Group</th>
              <th className="px-2 py-1.5 font-medium w-20">Status</th>
              <th className="px-2 py-1.5 font-medium">Note</th>
            </tr>
          </thead>
          <tbody>
            {detail.messages.map((m) => (
              <tr key={m.id} className="border-t">
                <td className="px-2 py-1.5 truncate max-w-[260px]">
                  {m.group_subject || m.jid}
                </td>
                <td className="px-2 py-1.5">
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
                <td className="px-2 py-1.5 text-muted-foreground truncate max-w-[200px]">
                  {m.error || ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="flex justify-end pt-2 border-t">
        <Button variant="outline" onClick={onClose}>
          {isTerminal ? "Close" : "Close (broadcast keeps running)"}
        </Button>
      </div>
    </div>
  );
}
