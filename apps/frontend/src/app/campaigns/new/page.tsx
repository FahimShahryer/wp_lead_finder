"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

// reddit + web are the core fetchable channels. meetup/eventbrite are public
// and auto-route to web. facebook/linkedin/twitter/x are anti-scraped — for
// those, stage 1 emits literal-phrase site: queries to harvest invite links
// from Google's snippets without trying to fetch the pages.
const PLATFORM_OPTIONS = [
  "reddit",
  "web",
  "meetup",
  "eventbrite",
  "facebook",
  "linkedin",
  "twitter",
  "x",
] as const;

function splitLines(s: string): string[] {
  return s
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
}

export default function NewCampaignPage() {
  const router = useRouter();
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [name, setName] = useState("");
  const [industries, setIndustries] = useState("AI agency owners\nMarketing agency owners");
  const [locations, setLocations] = useState("US\nUK\nDubai");
  const [negativeLocations, setNegativeLocations] = useState("India\nIndian");
  const [platforms, setPlatforms] = useState<string[]>(["reddit", "web"]);
  const [maxSerper, setMaxSerper] = useState(50);
  const [maxFirecrawl, setMaxFirecrawl] = useState(30);

  function togglePlatform(p: string) {
    setPlatforms((prev) => (prev.includes(p) ? prev.filter((x) => x !== p) : [...prev, p]));
  }

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      const resp = await api.createCampaign({
        name: name.trim(),
        industries: splitLines(industries),
        locations: splitLines(locations),
        negative_locations: splitLines(negativeLocations),
        platforms,
        max_credits_serper: maxSerper,
        max_credits_firecrawl: maxFirecrawl,
      });
      router.push(`/campaigns/${resp.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setSubmitting(false);
    }
  }

  return (
    <div className="mx-auto max-w-2xl space-y-4">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">New campaign</h1>
        <p className="text-sm text-muted-foreground">
          Define your ICP. The pipeline runs automatically once you submit.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Ideal Customer Profile</CardTitle>
        </CardHeader>
        <CardContent>
          <form onSubmit={onSubmit} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="name">Campaign name</Label>
              <Input
                id="name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Q2 AI agency owners — US/UK"
                required
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="industries">Industries (one per line)</Label>
              <Textarea
                id="industries"
                value={industries}
                onChange={(e) => setIndustries(e.target.value)}
                rows={3}
              />
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-2">
                <Label htmlFor="locations">Target locations (one per line)</Label>
                <Textarea
                  id="locations"
                  value={locations}
                  onChange={(e) => setLocations(e.target.value)}
                  rows={4}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="negative">Exclude (one per line)</Label>
                <Textarea
                  id="negative"
                  value={negativeLocations}
                  onChange={(e) => setNegativeLocations(e.target.value)}
                  rows={4}
                />
              </div>
            </div>

            <div className="space-y-2">
              <Label>Platforms</Label>
              <div className="flex flex-wrap gap-x-4 gap-y-2">
                {PLATFORM_OPTIONS.map((p) => (
                  <label key={p} className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={platforms.includes(p)}
                      onChange={() => togglePlatform(p)}
                      className="h-4 w-4"
                    />
                    {p}
                  </label>
                ))}
              </div>
              <p className="text-xs text-muted-foreground">
                reddit + web are core. meetup / eventbrite auto-route through Firecrawl.
                facebook / linkedin / twitter / x are anti-scraped — stage 1 will hunt for
                invite links in Google snippets only (no fetch attempted).
              </p>
            </div>

            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-2">
                <Label htmlFor="serper">Max Serper credits</Label>
                <Input
                  id="serper"
                  type="number"
                  min={1}
                  value={maxSerper}
                  onChange={(e) => setMaxSerper(parseInt(e.target.value || "0", 10))}
                />
                <p className="text-xs text-muted-foreground">
                  ~10 search results per credit. 50 → ~500 URLs to consider.
                </p>
              </div>
              <div className="space-y-2">
                <Label htmlFor="firecrawl">Max Firecrawl credits</Label>
                <Input
                  id="firecrawl"
                  type="number"
                  min={1}
                  value={maxFirecrawl}
                  onChange={(e) => setMaxFirecrawl(parseInt(e.target.value || "0", 10))}
                />
                <p className="text-xs text-muted-foreground">
                  Each non-Reddit URL costs 1 credit (cache hits are free).
                </p>
              </div>
            </div>

            {error && (
              <div className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
                {error}
              </div>
            )}

            <div className="flex justify-end gap-2">
              <Button
                type="button"
                variant="outline"
                onClick={() => router.push("/")}
                disabled={submitting}
              >
                Cancel
              </Button>
              <Button type="submit" disabled={submitting || !name.trim()}>
                {submitting ? "Submitting…" : "Run campaign"}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
