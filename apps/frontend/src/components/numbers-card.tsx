"use client";

import { useEffect, useRef, useState } from "react";
import useSWR, { mutate } from "swr";
import {
  CheckCircle2,
  Loader2,
  Megaphone,
  Phone,
  PlugZap,
  Plus,
  Trash2,
  X,
} from "lucide-react";

import { WaNumberStatus, WhatsAppNumber, api, fetcher } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { BroadcastModal } from "@/components/broadcast-modal";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

const NUMBERS_KEY = "/numbers";

const STATUS_LABEL: Record<WaNumberStatus, string> = {
  pending: "Starting…",
  qr_pending: "Scan QR",
  connecting: "Connecting…",
  connected: "Connected",
  disconnected: "Disconnected",
  logged_out: "Logged out",
};

const STATUS_VARIANT: Record<WaNumberStatus, string> = {
  pending: "bg-muted text-muted-foreground",
  qr_pending: "bg-amber-500/15 text-amber-700 dark:text-amber-300",
  connecting: "bg-blue-500/15 text-blue-700 dark:text-blue-300",
  connected: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
  disconnected: "bg-amber-500/15 text-amber-700 dark:text-amber-300",
  logged_out: "bg-destructive/15 text-destructive",
};

function formatMsisdn(msisdn: string | null): string {
  if (!msisdn) return "";
  // Strip everything except digits, then prefix +.
  const digits = msisdn.replace(/\D/g, "");
  return digits ? `+${digits}` : msisdn;
}

export function NumbersCard() {
  const { data: numbers } = useSWR<WhatsAppNumber[]>(NUMBERS_KEY, fetcher, {
    // Poll while any non-terminal session is in flight; throttle when all stable.
    refreshInterval: (latest) => {
      if (!latest) return 4000;
      return latest.some(
        (n) =>
          n.status === "qr_pending" ||
          n.status === "connecting" ||
          n.status === "pending",
      )
        ? 2000
        : 8000;
    },
  });

  const [showAdd, setShowAdd] = useState(false);
  const [broadcastFor, setBroadcastFor] = useState<WhatsAppNumber | null>(null);

  async function deleteNumber(id: number) {
    if (!confirm("Disconnect and remove this WhatsApp number?")) return;
    try {
      await api.deleteNumber(id);
      await mutate(NUMBERS_KEY);
    } catch (e) {
      alert(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <>
      <Card>
        <CardHeader className="flex flex-row items-start justify-between gap-2 space-y-0">
          <div>
            <CardTitle className="text-lg flex items-center gap-2">
              <PlugZap className="h-4 w-4" />
              WhatsApp numbers
            </CardTitle>
            <CardDescription>
              Linked accounts available for joining and messaging.
            </CardDescription>
          </div>
          <Button size="sm" onClick={() => setShowAdd(true)} className="h-8">
            <Plus className="mr-1 h-3.5 w-3.5" />
            Add number
          </Button>
        </CardHeader>
        <CardContent className="p-0">
          {numbers === undefined && (
            <div className="px-4 py-6 text-sm text-muted-foreground">Loading…</div>
          )}
          {numbers && numbers.length === 0 && (
            <div className="px-4 py-6 text-sm text-muted-foreground">
              No numbers linked yet. Click <span className="font-medium">Add number</span> to scan a QR.
            </div>
          )}
          {numbers && numbers.length > 0 && (
            <div className="divide-y">
              {numbers.map((n) => (
                <div
                  key={n.id}
                  className="flex items-center justify-between gap-3 px-4 py-3"
                >
                  <div className="flex items-center gap-3 min-w-0 flex-1">
                    <Phone className="h-4 w-4 text-muted-foreground shrink-0" />
                    <div className="min-w-0">
                      <div className="text-sm font-medium truncate">
                        {n.display_name}
                      </div>
                      <div className="text-xs text-muted-foreground truncate font-mono">
                        {formatMsisdn(n.msisdn) || "—"}
                      </div>
                    </div>
                  </div>
                  <span
                    className={
                      "rounded px-2 py-0.5 text-[11px] font-medium " +
                      (STATUS_VARIANT[n.status] || "bg-muted text-muted-foreground")
                    }
                  >
                    {STATUS_LABEL[n.status] || n.status}
                  </span>
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => setBroadcastFor(n)}
                    disabled={n.status !== "connected"}
                    title={
                      n.status === "connected"
                        ? "Broadcast to groups this number is in"
                        : "Number must be connected to broadcast"
                    }
                    className="h-7 px-2"
                  >
                    <Megaphone className="mr-1 h-3.5 w-3.5" />
                    Broadcast
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => deleteNumber(n.id)}
                    aria-label="Remove number"
                    className="h-7 px-2 text-muted-foreground hover:text-destructive"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {showAdd && (
        <AddNumberModal
          onClose={() => {
            setShowAdd(false);
            mutate(NUMBERS_KEY);
          }}
        />
      )}
      {broadcastFor && (
        <BroadcastModal
          number={broadcastFor}
          onClose={() => setBroadcastFor(null)}
        />
      )}
    </>
  );
}

// ---------- Add number modal ----------

function AddNumberModal({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState("");
  const [created, setCreated] = useState<WhatsAppNumber | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  // Once a number is created we poll its detail endpoint for QR refresh +
  // status updates until it's connected or we close the modal.
  useEffect(() => {
    if (!created) return;
    pollTimer.current = setInterval(async () => {
      try {
        const fresh = await api.getNumber(created.id);
        setCreated(fresh);
        if (fresh.status === "connected" || fresh.status === "logged_out") {
          if (pollTimer.current) clearInterval(pollTimer.current);
          pollTimer.current = null;
        }
      } catch {
        /* network blip — next tick will retry */
      }
    }, 1500);
    return () => {
      if (pollTimer.current) clearInterval(pollTimer.current);
      pollTimer.current = null;
    };
  }, [created?.id]);

  async function startAdd() {
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const n = await api.createNumber(name.trim());
      setCreated(n);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/60 backdrop-blur-sm p-4">
      <div className="w-full max-w-md rounded-lg border bg-card text-card-foreground shadow-lg">
        <div className="flex items-center justify-between border-b px-4 py-3">
          <h2 className="text-base font-semibold">Add WhatsApp number</h2>
          <button
            onClick={onClose}
            aria-label="Close"
            className="text-muted-foreground hover:text-foreground"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="p-4 space-y-4">
          {!created && (
            <>
              <div className="space-y-2">
                <Label htmlFor="display-name">Display name</Label>
                <Input
                  id="display-name"
                  placeholder="e.g. Bangladesh personal"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !busy && name.trim()) startAdd();
                  }}
                  autoFocus
                  disabled={busy}
                />
                <p className="text-xs text-muted-foreground">
                  A label only you'll see — helps when managing several numbers.
                </p>
              </div>
              {error && (
                <div className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive">
                  {error}
                </div>
              )}
              <Button
                onClick={startAdd}
                disabled={busy || !name.trim()}
                className="w-full"
              >
                {busy ? (
                  <>
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    Starting session…
                  </>
                ) : (
                  "Generate QR code"
                )}
              </Button>
            </>
          )}

          {created && (
            <QrPanel number={created} onDone={onClose} />
          )}
        </div>
      </div>
    </div>
  );
}

function QrPanel({
  number,
  onDone,
}: {
  number: WhatsAppNumber;
  onDone: () => void;
}) {
  const isConnected = number.status === "connected";
  const isLoggedOut = number.status === "logged_out";

  return (
    <div className="space-y-3 text-center">
      <div className="text-sm">
        <span className="font-medium">{number.display_name}</span>
        <div className="text-xs text-muted-foreground">
          Status: {STATUS_LABEL[number.status] || number.status}
        </div>
      </div>

      {isConnected ? (
        <div className="rounded-md border bg-emerald-500/5 p-6 space-y-2">
          <CheckCircle2 className="mx-auto h-10 w-10 text-emerald-600" />
          <div className="text-sm font-medium">Connected!</div>
          {number.msisdn && (
            <div className="text-xs text-muted-foreground font-mono">
              {formatMsisdn(number.msisdn)}
            </div>
          )}
          <Button onClick={onDone} className="mt-2 w-full" size="sm">
            Done
          </Button>
        </div>
      ) : isLoggedOut ? (
        <div className="rounded-md border border-destructive/40 bg-destructive/5 p-6 text-sm text-destructive">
          Session ended. Close and try again.
          <Button
            onClick={onDone}
            variant="outline"
            size="sm"
            className="mt-3 w-full"
          >
            Close
          </Button>
        </div>
      ) : number.qr_data_url ? (
        <div className="space-y-3">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={number.qr_data_url}
            alt="WhatsApp Web QR code"
            className="mx-auto h-64 w-64 rounded border bg-white p-2"
          />
          <div className="text-xs text-muted-foreground space-y-1">
            <div>Open WhatsApp on your phone</div>
            <div>
              Settings → <span className="font-medium">Linked devices</span> →{" "}
              <span className="font-medium">Link a device</span>
            </div>
            <div>Scan this QR. The window updates automatically.</div>
          </div>
        </div>
      ) : (
        <div className="rounded-md border bg-muted/30 p-8 text-sm text-muted-foreground flex items-center justify-center gap-2">
          <Loader2 className="h-4 w-4 animate-spin" />
          Waiting for QR…
        </div>
      )}
    </div>
  );
}
