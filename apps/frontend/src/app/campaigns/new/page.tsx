"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

// Only platforms that actually yield WhatsApp invites. Social platforms
// (facebook/linkedin/twitter/x) were removed after real-campaign data showed
// they consume query budget for ~0 yield: invites mostly aren't there, and
// the snippets get truncated before any link is visible. If a Facebook URL
// happens to surface from a regular open-web search and its snippet contains
// the invite, stage 3's snippet-hit path still extracts it for free — we just
// don't pay Serper credits hunting for them.
const PLATFORM_OPTIONS = [
  "reddit",
  "meetup",
  "eventbrite",
  "web",
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
  const [platforms, setPlatforms] = useState<string[]>([
    "reddit",
    "meetup",
    "eventbrite",
    "web",
  ]);
  // Which invite ecosystem this campaign hunts in. Each platform has its own
  // pipeline (queries, extract, validator) under apps/backend/src/pipeline/<p>/.
  const [platform, setPlatform] = useState<"whatsapp" | "discord" | "slack">(
    "whatsapp",
  );
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
        platform,
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
              <Label>Run campaign for</Label>
              <div className="flex gap-2">
                {(["whatsapp", "discord", "slack"] as const).map((p) => (
                  <button
                    key={p}
                    type="button"
                    onClick={() => setPlatform(p)}
                    className={
                      "flex-1 rounded-md border px-3 py-2 text-sm font-medium transition " +
                      (platform === p
                        ? "border-primary bg-primary text-primary-foreground"
                        : "border-input bg-background hover:bg-accent")
                    }
                  >
                    {p === "whatsapp"
                      ? "WhatsApp"
                      : p === "discord"
                        ? "Discord"
                        : "Slack"}
                  </button>
                ))}
              </div>
              <p className="text-xs text-muted-foreground">
                Each platform has its own pipeline — queries are anchored on{" "}
                <code>
                  {platform === "whatsapp"
                    ? "chat.whatsapp.com"
                    : platform === "discord"
                      ? "discord.gg"
                      : "join.slack.com"}
                </code>
                , and validation hits the{" "}
                {platform === "whatsapp"
                  ? "WhatsApp invite landing page (rate-limited, CAPTCHA-prone)"
                  : platform === "discord"
                    ? "Discord public API (no auth, fast)"
                    : "Slack public landing page (no API; tokens auto-expire ~30 days, so enrich soon)"}
                .
              </p>
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
                Reddit, Meetup, and Eventbrite each get targeted <code>site:</code> queries.
                Web means broad open-web queries with no site constraint (catches blogs,
                forums, newsletters). Social platforms (Facebook, LinkedIn, X) were removed
                — real-campaign data showed they cost query budget for ~0 yield.
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
