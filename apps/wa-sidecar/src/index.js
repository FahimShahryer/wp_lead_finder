// wa-sidecar — single-purpose Node service that owns Baileys WhatsApp
// connections. The FastAPI backend proxies REST calls here; the sidecar
// holds the long-lived WS sessions and persists creds to /data/sessions.
//
// Public endpoints (api → sidecar, internal docker network only):
//   POST   /sessions              { session_id?, display_name? } -> create or resume
//   GET    /sessions              -> list of all sessions in memory
//   GET    /sessions/:id          -> { session_id, status, qr_data_url?, msisdn? }
//   POST   /sessions/:id/logout   -> graceful logout, removes creds
//   GET    /healthz               -> { ok: true }

import {
  default as makeWASocket,
  Browsers,
  DisconnectReason,
  fetchLatestBaileysVersion,
  useMultiFileAuthState,
} from "@whiskeysockets/baileys";
import express from "express";
import { existsSync, mkdirSync, rmSync } from "fs";
import path from "path";
import pino from "pino";
import qrcode from "qrcode";
import { randomUUID } from "crypto";

const PORT = Number(process.env.PORT || 3001);
const SESSIONS_DIR = process.env.SESSIONS_DIR || "/data/sessions";
// Webhook config — used by the messages.upsert listener to push inbound
// messages to the FastAPI service. Empty values disable forwarding (sidecar
// keeps working for QR/send/etc., it just won't populate the inbox).
const API_URL = process.env.API_URL || "";
const INTERNAL_TOKEN = process.env.INTERNAL_TOKEN || "";

// Pino — INFO by default; DEBUG via env. Baileys' own logger is silent
// because it's extremely chatty at INFO+ and floods our logs.
const logger = pino({ level: process.env.LOG_LEVEL || "info" });
const baileysLogger = pino({ level: "silent" });

mkdirSync(SESSIONS_DIR, { recursive: true });

// In-memory state per session_id. Survives only while the sidecar process is
// up; session creds on disk survive restarts (resumed via resumeExisting()).
//
// Each entry: { sock, status, qr, qr_data_url, msisdn, display_name, last_updated }
const sessions = new Map();

function setStatus(sessionId, patch) {
  const cur = sessions.get(sessionId) || {};
  const next = { ...cur, ...patch, last_updated: Date.now() };
  sessions.set(sessionId, next);
  logger.info({ sessionId, status: next.status, msisdn: next.msisdn }, "status update");
  return next;
}

function publicView(entry, sessionId) {
  if (!entry) return null;
  return {
    session_id: sessionId,
    status: entry.status || "pending",
    qr_data_url: entry.qr_data_url || null,
    msisdn: entry.msisdn || null,
    display_name: entry.display_name || null,
    last_updated: entry.last_updated || null,
  };
}

function jidToMsisdn(jid) {
  // "8801711xxxxxxx:42@s.whatsapp.net" → "8801711xxxxxxx"
  if (!jid) return null;
  const left = String(jid).split("@")[0];
  return left.split(":")[0] || null;
}

function chatKind(jid) {
  if (!jid) return null;
  if (jid.endsWith("@g.us")) return "group";
  // WhatsApp issues TWO user-identity formats:
  //   - <phone>@s.whatsapp.net : phone-number identity (older default)
  //   - <lid>@lid              : linked-device anonymous identity (newer; used
  //                              when the contact hasn't shared their phone)
  // Both are 1-on-1 DMs from our perspective.
  if (jid.endsWith("@s.whatsapp.net") || jid.endsWith("@lid")) return "dm";
  return null;
}

function extractMessageBody(message) {
  // WhatsApp message can carry many shapes; we only render text in the inbox
  // and represent media with a placeholder so the user knows to open WA for
  // the attachment. Order matters — extendedTextMessage wraps replies/links,
  // conversation is plain text.
  if (!message) return "";
  if (message.conversation) return message.conversation;
  if (message.extendedTextMessage?.text) return message.extendedTextMessage.text;
  if (message.imageMessage) return `[image]${message.imageMessage.caption ? " " + message.imageMessage.caption : ""}`;
  if (message.videoMessage) return `[video]${message.videoMessage.caption ? " " + message.videoMessage.caption : ""}`;
  if (message.audioMessage) return "[audio]";
  if (message.documentMessage) return `[document] ${message.documentMessage.fileName || ""}`.trim();
  if (message.stickerMessage) return "[sticker]";
  if (message.locationMessage) return "[location]";
  if (message.contactMessage) return `[contact] ${message.contactMessage.displayName || ""}`.trim();
  if (message.reactionMessage) return `[reaction] ${message.reactionMessage.text || ""}`.trim();
  return "[unsupported message type]";
}

// In-memory group-subject cache so we don't call groupMetadata on every
// inbound message in a chatty group. Stale entries are refreshed lazily
// when older than CACHE_TTL.
const GROUP_SUBJECT_CACHE_TTL_MS = 5 * 60 * 1000;
const groupSubjectCache = new Map(); // jid -> { subject, fetchedAt }

async function getGroupSubject(sock, jid) {
  const cached = groupSubjectCache.get(jid);
  if (cached && Date.now() - cached.fetchedAt < GROUP_SUBJECT_CACHE_TTL_MS) {
    return cached.subject;
  }
  try {
    const meta = await sock.groupMetadata(jid);
    const subject = meta?.subject || null;
    groupSubjectCache.set(jid, { subject, fetchedAt: Date.now() });
    return subject;
  } catch (e) {
    logger.debug({ jid, err: e?.message }, "groupMetadata failed");
    // Cache the miss briefly to avoid hammering on permission errors.
    groupSubjectCache.set(jid, { subject: null, fetchedAt: Date.now() });
    return null;
  }
}

async function postInbound(payload) {
  if (!API_URL || !INTERNAL_TOKEN) return; // forwarding disabled
  try {
    const res = await fetch(`${API_URL}/internal/inbound`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Internal-Token": INTERNAL_TOKEN,
      },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      logger.warn(
        { status: res.status, body: text.slice(0, 200) },
        "inbound webhook non-2xx",
      );
    }
  } catch (e) {
    logger.warn({ err: e?.message }, "inbound webhook failed");
  }
}

// Fetched once at startup and reused for every session. WhatsApp servers
// reject sockets that advertise a stale Web version with status 405; this
// helper pulls the current version from Baileys' GitHub on first call and
// caches it in module-local state.
let cachedWaVersion = null;
async function getWaVersion() {
  if (cachedWaVersion) return cachedWaVersion;
  try {
    const { version, isLatest } = await fetchLatestBaileysVersion();
    logger.info({ version, isLatest }, "fetched WhatsApp Web version");
    cachedWaVersion = version;
    return version;
  } catch (e) {
    logger.warn({ err: e?.message }, "fetchLatestBaileysVersion failed; using bundled default");
    return undefined;
  }
}

async function startSession(sessionId, opts = {}) {
  const dir = path.join(SESSIONS_DIR, sessionId);
  mkdirSync(dir, { recursive: true });

  const { state, saveCreds } = await useMultiFileAuthState(dir);
  const version = await getWaVersion();
  const sock = makeWASocket({
    version,
    auth: state,
    logger: baileysLogger,
    printQRInTerminal: false,
    // Browsers.macOS("Desktop") is the canonical client identifier WhatsApp
    // expects from a desktop pairing flow. Custom strings (incl. our prior
    // ["Chrome (Linux)", "", ""]) get rejected at handshake on newer servers.
    browser: Browsers.macOS("Desktop"),
    syncFullHistory: false,
    markOnlineOnConnect: false,
  });

  setStatus(sessionId, {
    sock,
    status: state.creds.registered ? "connecting" : "qr_pending",
    display_name: opts.display_name ?? sessions.get(sessionId)?.display_name ?? null,
    qr: null,
    qr_data_url: null,
    msisdn: state.creds?.me?.id ? jidToMsisdn(state.creds.me.id) : null,
  });

  sock.ev.on("creds.update", saveCreds);

  sock.ev.on("messages.upsert", async (event) => {
    // type='notify' = freshly arriving messages. 'append' = history sync after
    // pairing; 'prepend' = older history. We only forward 'notify' to keep
    // the inbox feed bounded and avoid backfilling thousands of history rows.
    if (event.type !== "notify") return;
    for (const m of event.messages || []) {
      const remoteJid = m.key?.remoteJid;
      if (!remoteJid) continue;
      const fromMe = !!m.key?.fromMe;
      const messageId = m.key?.id;
      if (!messageId) continue;
      const kind = chatKind(remoteJid);
      if (!kind) {
        // status@broadcast, newsletter@newsletter, etc. — log so we can spot
        // any new jid scheme that needs to be handled.
        logger.debug({ remoteJid, sessionId }, "skipping message with unrecognized jid");
        continue;
      }
      const body = extractMessageBody(m.message);
      // Trim to avoid surprising payloads.
      const trimmed = String(body || "").slice(0, 4096);
      const ts = Number(m.messageTimestamp || 0) || Math.floor(Date.now() / 1000);
      // pushName is whoever SENT this individual message (the participant in
      // a group, the contact in a DM). Outbound messages from this socket
      // don't carry one — frontend renders "You".
      const senderName = !fromMe ? (m.pushName || null) : null;
      // chat_name is the title of the conversation:
      //   - for DMs we use pushName (the contact's display name) if available
      //   - for groups we look up the group subject (cached, 5-min TTL)
      let chatName = null;
      if (kind === "group") {
        chatName = await getGroupSubject(sock, remoteJid);
      } else if (kind === "dm") {
        chatName = m.pushName || null;
      }
      await postInbound({
        session_id: sessionId,
        wa_message_id: messageId,
        chat_jid: remoteJid,
        chat_name: chatName,
        kind,
        direction: fromMe ? "out" : "in",
        sender_jid: m.key?.participant || (fromMe ? null : remoteJid),
        sender_name: senderName,
        body: trimmed,
        ts_unix: ts,
      });
    }
  });

  sock.ev.on("connection.update", async (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      // Baileys re-emits a fresh QR until user scans (or timeout). Encode it
      // to a data URL so the frontend can drop it straight into <img src>.
      try {
        const qr_data_url = await qrcode.toDataURL(qr, { margin: 1, scale: 6 });
        setStatus(sessionId, { status: "qr_pending", qr, qr_data_url });
      } catch (e) {
        logger.error({ sessionId, err: e?.message }, "qr encode failed");
      }
    }

    if (connection === "open") {
      const me = sock?.user?.id || sock?.authState?.creds?.me?.id || null;
      setStatus(sessionId, {
        status: "connected",
        msisdn: me ? jidToMsisdn(me) : null,
        qr: null,
        qr_data_url: null,
      });
    }

    if (connection === "close") {
      // Boom-style errors expose the status code at output.statusCode. We read
      // it directly to avoid a separate @hapi/boom dependency.
      const reason = lastDisconnect?.error?.output?.statusCode;
      const loggedOut = reason === DisconnectReason.loggedOut;
      logger.info({ sessionId, reason, loggedOut }, "connection closed");

      if (loggedOut) {
        setStatus(sessionId, { status: "logged_out", qr: null, qr_data_url: null });
      } else {
        setStatus(sessionId, { status: "disconnected" });
        // Auto-reconnect after a short delay. Baileys' multi-device protocol
        // requires a fresh socket — we re-init from the persisted creds.
        setTimeout(() => {
          if (sessions.get(sessionId)?.status === "disconnected") {
            startSession(sessionId).catch((e) =>
              logger.error({ sessionId, err: e?.message }, "reconnect failed"),
            );
          }
        }, 2000);
      }
    }
  });

  return sock;
}

async function resumeExisting() {
  // On startup, walk SESSIONS_DIR and resume each persisted session so
  // restarts don't require re-pairing.
  if (!existsSync(SESSIONS_DIR)) return;
  const fs = await import("fs/promises");
  const entries = await fs.readdir(SESSIONS_DIR, { withFileTypes: true });
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const sessionId = entry.name;
    try {
      await startSession(sessionId);
      logger.info({ sessionId }, "resumed");
    } catch (e) {
      logger.error({ sessionId, err: e?.message }, "resume failed");
    }
  }
}

// ---------- HTTP API ----------

const app = express();
app.use(express.json());

app.get("/healthz", (_req, res) => res.json({ ok: true }));

app.post("/sessions", async (req, res) => {
  // Caller (api) supplies session_id so it can persist the row before
  // calling us. If omitted we generate one.
  const session_id = req.body?.session_id || randomUUID();
  const display_name = req.body?.display_name || null;

  if (sessions.has(session_id)) {
    return res.status(200).json(publicView(sessions.get(session_id), session_id));
  }
  try {
    await startSession(session_id, { display_name });
    return res.status(201).json(publicView(sessions.get(session_id), session_id));
  } catch (e) {
    logger.error({ session_id, err: e?.message }, "create failed");
    return res.status(500).json({ error: "session_create_failed", message: e?.message });
  }
});

app.get("/sessions", (_req, res) => {
  const list = [];
  for (const [sessionId, entry] of sessions) {
    list.push(publicView(entry, sessionId));
  }
  res.json({ sessions: list });
});

app.get("/sessions/:id", (req, res) => {
  const entry = sessions.get(req.params.id);
  if (!entry) return res.status(404).json({ error: "not_found" });
  res.json(publicView(entry, req.params.id));
});

app.get("/sessions/:id/groups", async (req, res) => {
  const id = req.params.id;
  const entry = sessions.get(id);
  if (!entry) return res.status(404).json({ error: "not_found" });
  if (entry.status !== "connected" || !entry.sock) {
    return res.status(409).json({ error: "not_connected", status: entry.status });
  }
  try {
    // Baileys helper — returns a map keyed by group JID. Each entry has
    // subject, participants, owner etc. We project to a small public shape.
    const map = await entry.sock.groupFetchAllParticipating();
    const groups = Object.values(map).map((g) => ({
      jid: g.id,
      subject: g.subject || "",
      participants_count: Array.isArray(g.participants) ? g.participants.length : 0,
      announce: !!g.announce, // admins-only? (we shouldn't try to send if true and we're not admin)
    }));
    return res.json({ groups });
  } catch (e) {
    logger.error({ id, err: e?.message }, "groups fetch failed");
    return res.status(500).json({ error: "groups_fetch_failed", message: e?.message });
  }
});

app.post("/sessions/:id/messages", async (req, res) => {
  const id = req.params.id;
  const entry = sessions.get(id);
  if (!entry) return res.status(404).json({ error: "not_found" });
  if (entry.status !== "connected" || !entry.sock) {
    return res.status(409).json({ error: "not_connected", status: entry.status });
  }
  const jid = req.body?.jid;
  const text = req.body?.text;
  if (!jid || typeof jid !== "string") {
    return res.status(400).json({ error: "missing_jid" });
  }
  if (!text || typeof text !== "string") {
    return res.status(400).json({ error: "missing_text" });
  }
  try {
    const sent = await entry.sock.sendMessage(jid, { text });
    return res.json({
      ok: true,
      message_id: sent?.key?.id || null,
      timestamp: sent?.messageTimestamp || null,
    });
  } catch (e) {
    logger.error({ id, jid, err: e?.message }, "send failed");
    return res.status(500).json({ error: "send_failed", message: e?.message });
  }
});

app.post("/sessions/:id/logout", async (req, res) => {
  const id = req.params.id;
  const entry = sessions.get(id);
  if (!entry) return res.status(404).json({ error: "not_found" });
  try {
    if (entry.sock) {
      try { await entry.sock.logout(); } catch { /* ignore */ }
      try { entry.sock.end(); } catch { /* ignore */ }
    }
  } finally {
    sessions.delete(id);
    const dir = path.join(SESSIONS_DIR, id);
    try { rmSync(dir, { recursive: true, force: true }); } catch { /* ignore */ }
  }
  res.json({ ok: true });
});

resumeExisting()
  .catch((e) => logger.error({ err: e?.message }, "resume sweep failed"))
  .finally(() => {
    app.listen(PORT, "0.0.0.0", () => {
      logger.info({ port: PORT, sessionsDir: SESSIONS_DIR }, "wa-sidecar listening");
    });
  });
