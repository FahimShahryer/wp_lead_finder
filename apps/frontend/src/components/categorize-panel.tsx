"use client";

import { useEffect, useState } from "react";
import { Download, Loader2, Sparkles, Trash2, X } from "lucide-react";

import { FilteredLead, LEAD_STATUSES, LeadStatus, api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";

type SavedCategory = {
  id: string;
  name: string;
  prompt: string;
  scope: LeadStatus | "all";
  leads: FilteredLead[];
  total_considered: number;
  dropped_invalid: number;
  created_at: number;
};

const storageKey = (cid: number) => `wp2:categories:${cid}`;

function loadCategories(cid: number): SavedCategory[] {
  if (typeof window === "undefined") return [];
  try {
    return JSON.parse(localStorage.getItem(storageKey(cid)) || "[]");
  } catch {
    return [];
  }
}

function saveCategories(cid: number, cats: SavedCategory[]) {
  localStorage.setItem(storageKey(cid), JSON.stringify(cats));
}

function csvEscape(v: string | number | null | undefined): string {
  if (v === null || v === undefined) return "";
  const s = String(v);
  if (/[",\n\r]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
  return s;
}

function buildCsv(category: SavedCategory): string {
  const header = [
    "rank",
    "group_name",
    "total_score",
    "relevance",
    "geo_fit",
    "engagement",
    "whatsapp_link",
    "source_url",
    "reason",
  ];
  const rows = category.leads.map((l, i) => [
    i + 1,
    l.group_name ?? "",
    l.total_score ?? "",
    l.relevance ?? "",
    l.geo_fit ?? "",
    l.engagement ?? "",
    `https://chat.whatsapp.com/${l.invite_id}`,
    l.source_url ?? "",
    l.reason,
  ]);
  return [header, ...rows]
    .map((r) => r.map(csvEscape).join(","))
    .join("\n");
}

function downloadCsv(category: SavedCategory) {
  const csv = buildCsv(category);
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const safe = category.name.replace(/[^a-z0-9-_]+/gi, "_").toLowerCase() || "category";
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${safe}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

type Scope = LeadStatus | "all";
const SCOPE_OPTIONS: Scope[] = ["pending", "approved", "joined", ...LEAD_STATUSES.filter(s => !["pending","approved","joined"].includes(s)), "all"];

export function CategorizePanel({ campaignId }: { campaignId: number }) {
  const [prompt, setPrompt] = useState("");
  const [scope, setScope] = useState<Scope>("pending");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<{
    prompt: string;
    scope: Scope;
    leads: FilteredLead[];
    total_considered: number;
    dropped_invalid: number;
  } | null>(null);
  const [categoryName, setCategoryName] = useState("");
  const [categories, setCategories] = useState<SavedCategory[]>([]);

  useEffect(() => {
    setCategories(loadCategories(campaignId));
  }, [campaignId]);

  const persist = (next: SavedCategory[]) => {
    setCategories(next);
    saveCategories(campaignId, next);
  };

  async function runFilter() {
    const trimmed = prompt.trim();
    if (!trimmed) return;
    setLoading(true);
    setError(null);
    setPreview(null);
    try {
      const res = await api.filterLeads(campaignId, trimmed, {
        status: scope === "all" ? null : scope,
      });
      setPreview({
        prompt: res.prompt,
        scope,
        leads: res.leads,
        total_considered: res.total_considered,
        dropped_invalid: res.dropped_invalid,
      });
      // Default the category name to the prompt itself; user can edit before saving.
      setCategoryName(trimmed);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  function saveCategory() {
    if (!preview) return;
    const name = categoryName.trim() || preview.prompt;
    const cat: SavedCategory = {
      id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      name,
      prompt: preview.prompt,
      scope: preview.scope,
      leads: preview.leads,
      total_considered: preview.total_considered,
      dropped_invalid: preview.dropped_invalid,
      created_at: Date.now(),
    };
    persist([cat, ...categories]);
    setPreview(null);
    setPrompt("");
    setCategoryName("");
  }

  function deleteCategory(id: string) {
    persist(categories.filter((c) => c.id !== id));
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-lg flex items-center gap-2">
          <Sparkles className="h-4 w-4" />
          Filter & categorize
        </CardTitle>
        <CardDescription>
          Describe a category in plain English. The LLM picks matching leads from this campaign;
          save each result as a downloadable CSV.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-col gap-2 sm:flex-row">
          <select
            value={scope}
            onChange={(e) => setScope(e.target.value as Scope)}
            disabled={loading}
            aria-label="Filter scope"
            className="h-10 rounded-md border border-input bg-background px-3 text-sm capitalize focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
          >
            {SCOPE_OPTIONS.map((s) => (
              <option key={s} value={s}>
                {s === "all" ? "All leads" : s}
              </option>
            ))}
          </select>
          <Input
            placeholder='e.g. "AI and machine learning groups", "Dubai-based business communities"'
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !loading) runFilter();
            }}
            disabled={loading}
          />
          <Button onClick={runFilter} disabled={loading || !prompt.trim()}>
            {loading ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Classifying…
              </>
            ) : (
              "Filter"
            )}
          </Button>
        </div>

        {error && (
          <div className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
            {error}
          </div>
        )}

        {preview && (
          <div className="rounded-md border bg-muted/30 p-3 space-y-3">
            <div className="flex items-center justify-between gap-2">
              <div className="text-sm">
                <span className="font-semibold">{preview.leads.length}</span>
                <span className="text-muted-foreground">
                  {" "}
                  valid of {preview.total_considered}
                  {preview.dropped_invalid > 0 && (
                    <>
                      {" "}
                      <span className="text-amber-600 dark:text-amber-400">
                        · {preview.dropped_invalid} dropped (dead invite)
                      </span>
                    </>
                  )}
                  {" "}—{" "}
                </span>
                <span className="italic">"{preview.prompt}"</span>
              </div>
              <button
                aria-label="Discard preview"
                onClick={() => setPreview(null)}
                className="text-muted-foreground hover:text-foreground shrink-0"
              >
                <X className="h-4 w-4" />
              </button>
            </div>

            {preview.leads.length === 0 ? (
              <div className="text-sm text-muted-foreground">
                No leads matched. Try a different prompt.
              </div>
            ) : (
              <>
                <div className="max-h-64 overflow-y-auto rounded border bg-background">
                  <table className="w-full text-xs">
                    <thead className="sticky top-0 bg-muted text-left">
                      <tr>
                        <th className="px-2 py-1.5 font-medium">Score</th>
                        <th className="px-2 py-1.5 font-medium">Group</th>
                        <th className="px-2 py-1.5 font-medium">Reason</th>
                      </tr>
                    </thead>
                    <tbody>
                      {preview.leads.map((l) => (
                        <tr key={l.id} className="border-t">
                          <td className="px-2 py-1.5 tabular-nums">{l.total_score ?? "—"}</td>
                          <td className="px-2 py-1.5">
                            <a
                              href={`https://chat.whatsapp.com/${l.invite_id}`}
                              target="_blank"
                              rel="noreferrer"
                              className="hover:underline font-medium"
                            >
                              {l.group_name ?? l.invite_id.slice(0, 14) + "…"}
                            </a>
                          </td>
                          <td className="px-2 py-1.5 text-muted-foreground">{l.reason}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>

                <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
                  <Input
                    placeholder="Category name (e.g. AI groups)"
                    value={categoryName}
                    onChange={(e) => setCategoryName(e.target.value)}
                  />
                  <Button onClick={saveCategory} variant="default">
                    Save category
                  </Button>
                </div>
              </>
            )}
          </div>
        )}

        {categories.length > 0 && (
          <div className="space-y-2">
            <div className="text-xs uppercase tracking-wider text-muted-foreground">
              Saved categories
            </div>
            <div className="space-y-1.5">
              {categories.map((c) => (
                <div
                  key={c.id}
                  className="flex items-center justify-between rounded-md border bg-background px-3 py-2"
                >
                  <div className="min-w-0 flex-1">
                    <div className="font-medium text-sm truncate">{c.name}</div>
                    <div className="text-xs text-muted-foreground truncate">
                      {c.leads.length} leads · scope: <span className="capitalize">{c.scope ?? "pending"}</span> · "{c.prompt}"
                    </div>
                  </div>
                  <div className="flex items-center gap-1">
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => downloadCsv(c)}
                      disabled={c.leads.length === 0}
                    >
                      <Download className="mr-1 h-3.5 w-3.5" />
                      CSV
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => deleteCategory(c.id)}
                      aria-label="Delete category"
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
