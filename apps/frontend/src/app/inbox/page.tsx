"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import useSWR, { mutate } from "swr";
import {
  ArrowLeft,
  Inbox as InboxIcon,
  Loader2,
  Search,
  Send,
  ShieldAlert,
  Users,
  User as UserIcon,
} from "lucide-react";

import {
  ChatKind,
  Conversation,
  ConversationMessage,
  WhatsAppNumber,
  api,
  fetcher,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

const TERMINAL_NUMBER = new Set<string>(["logged_out"]);

type KindFilter = "all" | "unread" | "groups" | "dms";

function formatPhone(msisdn: string | null): string {
  if (!msisdn) return "";
  const d = msisdn.replace(/\D/g, "");
  return d ? `+${d}` : msisdn;
}

// WhatsApp uses two user-identity formats. The @lid one is NOT a phone number
// — rendering it as +<digits> creates a fake phone that misleads the user.
function jidIdentity(jid: string): {
  kind: "phone" | "lid" | "group" | "other";
  value: string;
} {
  if (jid.endsWith("@s.whatsapp.net"))
    return { kind: "phone", value: jid.split("@")[0] };
  if (jid.endsWith("@lid"))
    return { kind: "lid", value: jid.split("@")[0] };
  if (jid.endsWith("@g.us"))
    return { kind: "group", value: jid.split("@")[0] };
  return { kind: "other", value: jid };
}

function relativeTime(iso: string | null): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  const now = Date.now();
  const sec = Math.max(1, Math.round((now - then) / 1000));
  if (sec < 60) return `${sec}s`;
  const min = Math.round(sec / 60);
  if (min < 60) return `${min}m`;
  const hr = Math.round(min / 60);
  if (hr < 24) return `${hr}h`;
  const day = Math.round(hr / 24);
  if (day < 7) return `${day}d`;
  return new Date(iso).toLocaleDateString();
}

// Hash a string into a stable hue 0-359 — used to color sender names in
// group threads consistently per-person, like WhatsApp Web does.
function hueFor(seed: string): number {
  let h = 0;
  for (let i = 0; i < seed.length; i++) h = (h * 31 + seed.charCodeAt(i)) % 360;
  return h;
}

function initialsOf(name: string): string {
  const trimmed = (name || "").trim();
  if (!trimmed) return "?";
  const parts = trimmed.split(/\s+/);
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

export default function InboxPage() {
  const [filter, setFilter] = useState<KindFilter>("all");
  const [numberFilter, setNumberFilter] = useState<number | "all">("all");
  const [search, setSearch] = useState("");
  const [activeId, setActiveId] = useState<number | null>(null);

  const { data: numbers } = useSWR<WhatsAppNumber[]>("/numbers", fetcher);

  // Server filters: kind=group/dm + number_id. 'unread' is applied
  // client-side because the server doesn't have an unread filter.
  const params = new URLSearchParams();
  if (filter === "groups") params.set("kind", "group");
  if (filter === "dms") params.set("kind", "dm");
  if (numberFilter !== "all") params.set("number_id", String(numberFilter));
  const convsKey = `/conversations${params.toString() ? `?${params}` : ""}`;
  const { data: conversations } = useSWR<Conversation[]>(convsKey, fetcher, {
    refreshInterval: 3000,
  });

  const visible = useMemo(() => {
    if (!conversations) return undefined;
    let list = conversations;
    if (filter === "unread") list = list.filter((c) => c.unread_count > 0);
    const q = search.trim().toLowerCase();
    if (q) {
      list = list.filter(
        (c) =>
          (c.name || c.jid).toLowerCase().includes(q) ||
          (c.last_message_preview || "").toLowerCase().includes(q),
      );
    }
    return list;
  }, [conversations, filter, search]);

  const active = useMemo(
    () => conversations?.find((c) => c.id === activeId) ?? null,
    [conversations, activeId],
  );

  const kindCounts = useMemo(() => {
    if (!conversations) return { all: 0, unread: 0, groups: 0, dms: 0 };
    return {
      all: conversations.length,
      unread: conversations.filter((c) => c.unread_count > 0).length,
      groups: conversations.filter((c) => c.kind === "group").length,
      dms: conversations.filter((c) => c.kind === "dm").length,
    };
  }, [conversations]);

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <Link
            href="/"
            className="text-xs text-muted-foreground hover:underline"
          >
            <ArrowLeft className="mr-1 inline h-3 w-3" />
            Dashboard
          </Link>
          <h1 className="mt-1 flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <InboxIcon className="h-5 w-5" />
            Shared inbox
          </h1>
          <p className="text-sm text-muted-foreground">
            Inbound and outbound messages across all linked numbers, including replies you send from your phone.
          </p>
        </div>
      </div>

      {/* Two-pane layout, WA-Web-shaped */}
      <div className="grid gap-3 lg:grid-cols-[360px_1fr] min-h-[70vh]">
        {/* Left pane: filters + search + conversation list */}
        <div className="rounded-md border bg-card overflow-hidden flex flex-col">
          <div className="border-b px-3 py-2 space-y-2">
            <div className="relative">
              <Search className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <Input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search chats…"
                className="pl-8 h-9"
              />
            </div>

            <div className="flex flex-wrap gap-1">
              {(["all", "unread", "groups", "dms"] as const).map((k) => {
                const sel = filter === k;
                const count = kindCounts[k];
                const label = k === "dms" ? "DMs" : k[0].toUpperCase() + k.slice(1);
                return (
                  <button
                    key={k}
                    onClick={() => setFilter(k)}
                    className={
                      "rounded-full px-2.5 py-1 text-[11px] font-medium transition-colors " +
                      (sel
                        ? "bg-primary text-primary-foreground"
                        : "bg-muted text-muted-foreground hover:bg-muted/80")
                    }
                  >
                    {label}
                    <span
                      className={
                        "ml-1 tabular-nums " +
                        (sel ? "opacity-90" : "opacity-60")
                      }
                    >
                      {count}
                    </span>
                  </button>
                );
              })}
            </div>

            {numbers && numbers.length > 1 && (
              <div className="flex flex-wrap gap-1 text-[10px] uppercase tracking-wider text-muted-foreground items-center">
                <span className="mr-1">From:</span>
                <button
                  onClick={() => setNumberFilter("all")}
                  className={
                    "rounded px-2 py-0.5 normal-case " +
                    (numberFilter === "all"
                      ? "bg-foreground text-background"
                      : "hover:bg-muted")
                  }
                >
                  All numbers
                </button>
                {numbers.map((n) => (
                  <button
                    key={n.id}
                    onClick={() => setNumberFilter(n.id)}
                    disabled={TERMINAL_NUMBER.has(n.status)}
                    className={
                      "rounded px-2 py-0.5 normal-case " +
                      (numberFilter === n.id
                        ? "bg-foreground text-background"
                        : "hover:bg-muted")
                    }
                  >
                    {n.display_name}
                  </button>
                ))}
              </div>
            )}
          </div>

          <ConversationList
            conversations={visible}
            activeId={activeId}
            onPick={(id) => {
              setActiveId(id);
              api.markConversationRead(id).catch(() => {});
              mutate(convsKey);
            }}
          />
        </div>

        {/* Right pane: active thread */}
        <div className="rounded-md border bg-card flex flex-col min-h-[70vh]">
          {active ? (
            <Thread conversation={active} />
          ) : (
            <div className="flex-1 flex flex-col items-center justify-center text-sm text-muted-foreground gap-2 p-8">
              <InboxIcon className="h-10 w-10 opacity-30" />
              {conversations && conversations.length === 0
                ? "No conversations yet. Messages will land here as they arrive."
                : "Pick a conversation on the left."}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------- Conversation list ----------

function Avatar({
  name,
  kind,
}: {
  name: string;
  kind: ChatKind;
}) {
  const hue = hueFor(name || kind);
  const bg = `hsl(${hue} 55% 45%)`;
  if (kind === "group") {
    return (
      <div
        className="h-10 w-10 rounded-md flex items-center justify-center text-white shrink-0"
        style={{ backgroundColor: bg }}
        aria-label="Group"
      >
        <Users className="h-5 w-5" />
      </div>
    );
  }
  return (
    <div
      className="h-10 w-10 rounded-full flex items-center justify-center text-white text-xs font-semibold shrink-0"
      style={{ backgroundColor: bg }}
      aria-label="Direct message"
    >
      {initialsOf(name)}
    </div>
  );
}

function ConversationList({
  conversations,
  activeId,
  onPick,
}: {
  conversations: Conversation[] | undefined;
  activeId: number | null;
  onPick: (id: number) => void;
}) {
  if (!conversations) {
    return (
      <div className="p-4 text-xs text-muted-foreground flex items-center gap-2">
        <Loader2 className="h-3 w-3 animate-spin" /> Loading…
      </div>
    );
  }
  if (conversations.length === 0) {
    return (
      <div className="p-4 text-xs text-muted-foreground">
        No conversations match.
      </div>
    );
  }
  return (
    <div className="overflow-y-auto divide-y">
      {conversations.map((c) => {
        const active = c.id === activeId;
        const displayName = c.name || c.jid.split("@")[0];
        return (
          <button
            key={c.id}
            onClick={() => onPick(c.id)}
            className={
              "w-full text-left px-3 py-2.5 flex items-start gap-3 transition-colors " +
              (active ? "bg-muted" : "hover:bg-muted/40")
            }
          >
            <Avatar name={displayName} kind={c.kind} />
            <div className="min-w-0 flex-1">
              <div className="flex items-center justify-between gap-2">
                <span className="text-sm font-semibold truncate">
                  {displayName}
                </span>
                <span className="text-[10px] text-muted-foreground shrink-0 tabular-nums">
                  {relativeTime(c.last_message_at)}
                </span>
              </div>
              <div className="flex items-center justify-between gap-2 mt-0.5">
                <span className="text-xs text-muted-foreground truncate">
                  {c.last_message_preview || (
                    <span className="italic opacity-70">No messages</span>
                  )}
                </span>
                {c.unread_count > 0 && (
                  <span className="rounded-full bg-emerald-500 text-white text-[10px] font-semibold px-1.5 py-0.5 min-w-[20px] text-center shrink-0">
                    {c.unread_count}
                  </span>
                )}
              </div>
              <div className="flex items-center gap-1.5 mt-1">
                <span
                  className={
                    "inline-flex items-center gap-0.5 rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wider font-bold " +
                    (c.kind === "group"
                      ? "bg-blue-500/20 text-blue-800 dark:text-blue-200 ring-1 ring-blue-500/40"
                      : "bg-emerald-500/20 text-emerald-800 dark:text-emerald-200 ring-1 ring-emerald-500/40")
                  }
                >
                  {c.kind === "group" ? (
                    <>
                      <Users className="h-2.5 w-2.5" />
                      Group
                    </>
                  ) : (
                    <>
                      <UserIcon className="h-2.5 w-2.5" />
                      DM
                    </>
                  )}
                </span>
                <span className="text-[10px] text-muted-foreground/80 truncate">
                  via {c.number_display_name}
                </span>
              </div>
            </div>
          </button>
        );
      })}
    </div>
  );
}

// ---------- Active thread ----------

function Thread({ conversation }: { conversation: Conversation }) {
  const messagesKey = `/conversations/${conversation.id}/messages`;
  const { data: messages } = useSWR<ConversationMessage[]>(messagesKey, fetcher, {
    refreshInterval: 3000,
  });

  const [reply, setReply] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom when message count changes.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [messages?.length]);

  async function send() {
    const trimmed = reply.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    setErr(null);
    try {
      await api.sendReply(conversation.id, trimmed);
      setReply("");
      await mutate(messagesKey);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const displayName = conversation.name || conversation.jid.split("@")[0];
  const isGroup = conversation.kind === "group";

  return (
    <div className="flex flex-col flex-1 min-h-0">
      {/* Header */}
      <div className="border-b px-4 py-3 flex items-center gap-3 bg-muted/30">
        <Avatar name={displayName} kind={conversation.kind} />
        <div className="min-w-0 flex-1">
          <div className="text-[10px] uppercase tracking-wider font-bold mb-0.5">
            <span
              className={
                isGroup
                  ? "text-blue-700 dark:text-blue-300"
                  : "text-emerald-700 dark:text-emerald-300"
              }
            >
              {isGroup ? "▣ Group chat" : "● Direct message"}
            </span>
          </div>
          <div className="flex items-center gap-2">
            <span className="text-sm font-semibold truncate">{displayName}</span>
          </div>
          <div className="text-[11px] text-muted-foreground truncate">
            {(() => {
              const id = jidIdentity(conversation.jid);
              if (id.kind === "phone") {
                return <span className="font-mono">{formatPhone(id.value)}</span>;
              }
              if (id.kind === "lid") {
                return (
                  <span
                    className="italic"
                    title="WhatsApp LID — the contact hasn't shared their phone number"
                  >
                    private contact (no phone shared)
                  </span>
                );
              }
              // group / other → just show raw jid as before
              return <span className="font-mono">{conversation.jid}</span>;
            })()}
            {" · via "}
            <span className="text-foreground">
              {conversation.number_display_name}
            </span>
          </div>
        </div>
      </div>

      {/* Messages */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto px-4 py-4 space-y-1.5 bg-[radial-gradient(ellipse_at_top,_var(--tw-gradient-stops))] from-muted/10 to-transparent">
        {!messages && (
          <div className="text-xs text-muted-foreground flex items-center gap-2">
            <Loader2 className="h-3 w-3 animate-spin" /> Loading messages…
          </div>
        )}
        {messages && messages.length === 0 && (
          <div className="text-xs text-muted-foreground">
            No messages yet in this conversation.
          </div>
        )}
        {messages?.map((m, idx) => {
          // Group consecutive messages from the same author so we only render
          // the sender label on the first bubble (matches WA Web behavior).
          const prev = messages[idx - 1];
          const sameAuthor =
            prev &&
            prev.direction === m.direction &&
            (prev.sender_jid || "") === (m.sender_jid || "");
          return (
            <MessageBubble
              key={m.id}
              m={m}
              isGroup={isGroup}
              groupName={displayName}
              showSender={isGroup && !sameAuthor}
            />
          );
        })}
      </div>

      {/* Reply box + ban-prevention reminder */}
      <div className="border-t p-3 space-y-2 bg-muted/20">
        <BanReminder />
        <div className="flex items-end gap-2">
          <Textarea
            value={reply}
            onChange={(e) => setReply(e.target.value)}
            placeholder="Type a reply…"
            rows={2}
            className="resize-none"
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                send();
              }
            }}
          />
          <Button onClick={send} disabled={busy || !reply.trim()}>
            {busy ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <>
                <Send className="mr-1 h-4 w-4" /> Send
              </>
            )}
          </Button>
        </div>
        {err && <div className="text-xs text-destructive">{err}</div>}
        <div className="text-[10px] text-muted-foreground">
          ⌘/Ctrl + Enter to send
        </div>
      </div>
    </div>
  );
}

function MessageBubble({
  m,
  isGroup,
  groupName,
  showSender,
}: {
  m: ConversationMessage;
  isGroup: boolean;
  groupName: string;
  showSender: boolean;
}) {
  const isOut = m.direction === "out";
  // Color sender names consistently per person across the thread (WA-Web style).
  const senderHue = hueFor(m.sender_jid || m.sender_name || "");
  const senderColor = `hsl(${senderHue} 65% 40%)`;
  return (
    <div className={"flex " + (isOut ? "justify-end" : "justify-start")}>
      <div
        className={
          "max-w-[78%] rounded-lg px-3 py-2 text-sm whitespace-pre-wrap break-words " +
          (isOut
            ? "bg-emerald-600 text-white rounded-br-sm"
            : "bg-card border text-foreground rounded-bl-sm")
        }
      >
        {showSender && !isOut && (m.sender_name || m.sender_jid) && (
          <div
            className="text-[11px] font-semibold mb-0.5 flex items-baseline gap-1.5"
            style={{ color: senderColor }}
          >
            <span>
              {m.sender_name || (m.sender_jid?.split("@")[0] ?? "Unknown")}
            </span>
            {isGroup && (
              <span className="text-[9px] font-normal opacity-70 uppercase tracking-wider">
                in {groupName}
              </span>
            )}
          </div>
        )}
        <div>{m.body || <span className="opacity-60">[empty]</span>}</div>
        <div
          className={
            "mt-1 text-[10px] " +
            (isOut ? "text-emerald-50/80" : "text-muted-foreground")
          }
        >
          {new Date(m.ts).toLocaleTimeString([], {
            hour: "2-digit",
            minute: "2-digit",
          })}
          {isOut && m.status && m.status !== "sent" && ` · ${m.status}`}
        </div>
      </div>
    </div>
  );
}

// ---------- Ban-prevention reminder ----------

export function BanReminder() {
  return (
    <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2.5 text-[11px] text-amber-900 dark:text-amber-200">
      <div className="flex items-start gap-2">
        <ShieldAlert className="h-3.5 w-3.5 shrink-0 mt-0.5" />
        <div className="space-y-1">
          <div className="font-semibold">Avoid getting banned</div>
          <ul className="list-disc list-inside space-y-0.5 marker:text-amber-700/60">
            <li>Vary your wording — don&apos;t copy-paste identical replies.</li>
            <li>
              Wait a beat between sends. WhatsApp&apos;s ML flags burst patterns from one number.
            </li>
            <li>
              Don&apos;t cold-DM users who haven&apos;t engaged with your group first.
            </li>
            <li>
              Stay under ~80–100 outbound msgs / number / day until you&apos;ve warmed it up.
            </li>
          </ul>
        </div>
      </div>
    </div>
  );
}
