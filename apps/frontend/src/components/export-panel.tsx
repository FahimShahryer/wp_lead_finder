"use client";

import { useState } from "react";
import { Download, Loader2 } from "lucide-react";

import { LEAD_STATUSES, LeadStatus, Tag } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

type ExportFilter = {
  statuses: LeadStatus[] | null;
  tag_ids: number[] | null;
  min_total_score: number | null;
  only_scored: boolean;
  only_valid_invites: boolean;
};

export function ExportPanel({
  campaignId,
  campaignName,
  tags,
}: {
  campaignId: number;
  campaignName: string;
  tags: Tag[] | undefined;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [statuses, setStatuses] = useState<Set<LeadStatus>>(new Set());
  const [tagIds, setTagIds] = useState<Set<number>>(new Set());
  const [minScore, setMinScore] = useState<string>("");
  const [onlyScored, setOnlyScored] = useState(true);
  const [onlyValid, setOnlyValid] = useState(true);

  function toggleStatus(s: LeadStatus) {
    setStatuses((prev) => {
      const next = new Set(prev);
      if (next.has(s)) next.delete(s);
      else next.add(s);
      return next;
    });
  }

  function toggleTag(id: number) {
    setTagIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function downloadCsv() {
    setBusy(true);
    setError(null);
    try {
      const filter: ExportFilter = {
        statuses: statuses.size > 0 ? Array.from(statuses) : null,
        tag_ids: tagIds.size > 0 ? Array.from(tagIds) : null,
        min_total_score: minScore ? Number(minScore) : null,
        only_scored: onlyScored,
        only_valid_invites: onlyValid,
      };
      const r = await fetch(`${API_URL}/campaigns/${campaignId}/export.csv`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(filter),
      });
      if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      const safe = campaignName.replace(/[^a-z0-9-_]+/gi, "_").toLowerCase() || "campaign";
      a.download = `${safe}_leads.csv`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  if (!open) {
    return (
      <Button
        size="sm"
        variant="outline"
        onClick={() => setOpen(true)}
        className="h-8"
      >
        <Download className="mr-1 h-3.5 w-3.5" />
        Export…
      </Button>
    );
  }

  return (
    <div className="rounded-md border bg-muted/30 p-3 space-y-3 min-w-[280px]">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wider">Export filters</span>
        <button
          onClick={() => setOpen(false)}
          className="text-xs text-muted-foreground hover:underline"
        >
          close
        </button>
      </div>

      <div className="space-y-2">
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
          Status (any selected; empty = all)
        </div>
        <div className="flex flex-wrap gap-1">
          {LEAD_STATUSES.map((s) => {
            const active = statuses.has(s);
            return (
              <button
                key={s}
                onClick={() => toggleStatus(s)}
                className={
                  "rounded border px-2 py-0.5 text-[11px] font-medium capitalize " +
                  (active
                    ? "border-primary bg-primary text-primary-foreground"
                    : "border-border bg-background text-muted-foreground hover:bg-muted")
                }
              >
                {s}
              </button>
            );
          })}
        </div>
      </div>

      {tags && tags.length > 0 && (
        <div className="space-y-2">
          <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
            Tags (any selected; empty = all)
          </div>
          <div className="flex flex-wrap gap-1">
            {tags.map((t) => {
              const active = tagIds.has(t.id);
              return (
                <button
                  key={t.id}
                  onClick={() => toggleTag(t.id)}
                  className={
                    "rounded-full border px-2 py-0.5 text-[11px] font-medium " +
                    (active
                      ? "border-primary bg-primary text-primary-foreground"
                      : "border-border bg-background text-muted-foreground hover:bg-muted")
                  }
                >
                  {t.name}
                </button>
              );
            })}
          </div>
        </div>
      )}

      <div className="space-y-2">
        <div className="text-[10px] uppercase tracking-wider text-muted-foreground">
          Minimum total score
        </div>
        <Input
          type="number"
          min={0}
          max={100}
          placeholder="0"
          value={minScore}
          onChange={(e) => setMinScore(e.target.value)}
          className="h-8 w-24"
        />
      </div>

      <div className="space-y-1.5">
        <label className="flex items-center gap-2 text-xs">
          <input
            type="checkbox"
            checked={onlyScored}
            onChange={(e) => setOnlyScored(e.target.checked)}
          />
          Only scored leads
        </label>
        <label className="flex items-center gap-2 text-xs">
          <input
            type="checkbox"
            checked={onlyValid}
            onChange={(e) => setOnlyValid(e.target.checked)}
          />
          Only valid WhatsApp invites
        </label>
      </div>

      {error && (
        <div className="rounded border border-destructive/40 bg-destructive/5 p-2 text-xs text-destructive">
          {error}
        </div>
      )}

      <Button onClick={downloadCsv} disabled={busy} className="w-full h-8">
        {busy ? (
          <>
            <Loader2 className="mr-2 h-3.5 w-3.5 animate-spin" />
            Building CSV…
          </>
        ) : (
          <>
            <Download className="mr-1 h-3.5 w-3.5" />
            Download CSV
          </>
        )}
      </Button>
    </div>
  );
}
