# wp2 — WhatsApp AI Ops Platform — Architecture

End-to-end stack for finding WhatsApp groups matching an Ideal Customer Profile, validating + categorizing the invites, and running outreach (broadcast + reply) through one or more WhatsApp accounts paired via QR.

This file is the **canonical map of the system**. A fresh agent should be able to read this top-to-bottom and know where every concept lives in the codebase without having to explore. File paths are absolute from the repo root.

---

## 1. What the app actually does

| Capability | Module from the MVP PDF | Status |
|---|---|---|
| Configure a campaign (industries, locations, platforms, budgets) | 1 — Criteria Form | ✅ done |
| Discover WhatsApp invite links via Google + Reddit + Firecrawl | 2 — Link Database Builder | ✅ done |
| LLM-score each invite vs. ICP, validate the link is alive, fetch the real group name | 3 — Validation & Enrichment | ✅ done |
| Tag groups (industry / business / misc), filter, mark approved/joined/contacted/etc., flexible CSV export | 4 — Group Organizer | ✅ done |
| Pair WhatsApp accounts via QR, manage multiple linked numbers | 7 — WhatsApp Login (QR) | ✅ done |
| Send the same message to many groups with throttling | 5 — Group Message Sending | ✅ done |
| Unified inbox for incoming + outgoing messages across all linked numbers | 6 — Shared Inbox | ✅ done |
| User accounts / per-user permissions | 8 — Permission & Access Control | ⏸️ deferred (single-user) |
| Broken Link Recovery, Auto-join Campaigns, Spintax, Analytics, Mobile | — | ❌ excluded per PDF |

The current usage pattern is: **single user, multi-number**. Auth is intentionally not wired — when teammates come on, Module 8 is the next add and slots in cleanly.

---

## 2. Topology (Docker Compose)

`/Users/saidurrahaman/Desktop/ai/wp_lead_finder/docker-compose.yml` defines six services on one internal network:

```
                    ┌─────────────────────────────┐
                    │   Browser (localhost:3000)  │
                    └──────────────┬──────────────┘
                                   │ Next.js dev (SWR polling 1.5s–8s)
                                   ▼
                    ┌─────────────────────────────┐
                    │  frontend  Next.js 15 + Tailwind │
                    └──────────────┬──────────────┘
                                   │ HTTP (CORS allowed: 3000 → 8000)
                                   ▼
       ┌────────────────────────────────────────────────┐
       │  api  FastAPI + uvicorn  (host: 8000)          │
       │  - REST API, lifespan-managed redis + arq pool │
       │  - validates, enqueues, mediates sidecar calls │
       └─────┬──────────────┬─────────────┬─────────────┘
             │              │             │
             │ asyncpg      │ Redis       │ HTTP
             ▼              ▼             ▼
    ┌────────────────┐ ┌─────────┐ ┌──────────────────────┐
    │  postgres 16   │ │ redis 7 │ │ wa-sidecar  Node 20  │
    │  (host: 5432)  │ │ (6379)  │ │  Express + Baileys   │
    │  postgres_data │ │ in-mem  │ │  internal port 3001  │
    └────────────────┘ └────┬────┘ └─────────┬────────────┘
                            │ arq pop        │ WSS to Meta
                            ▼                ▼
                    ┌──────────────┐    ┌─────────────────┐
                    │  worker arq  │    │ WhatsApp servers│
                    │ (no host port)│    │ (Meta cloud)    │
                    └──────┬────────┘    └─────────────────┘
                           │ HTTP (sidecar.send_message during broadcasts)
                           └────────► wa-sidecar
```

**No service is exposed publicly except 3000 (browser) and 8000 (api).** Postgres and Redis are bound to host ports for dev convenience but never accept anything but localhost. `wa-sidecar` is internal-only.

### Why split api and sidecar
- Baileys is Node-only. FastAPI is Python.
- Process isolation: a Baileys panic / OOM / version-mismatch crash can't take down the api.
- Each container restarts independently; sidecar resumes WhatsApp sessions from disk creds on reboot.

### Volumes
- `postgres_data` — Postgres datadir
- `wa_sessions` — Baileys multi-file auth state (`/data/sessions/<session_id>/...` per linked number). Survives restarts; not host-bound (so encrypted creds don't leak into git).

---

## 3. Tech stack

| Layer | Choice | Why |
|---|---|---|
| Frontend | Next.js 15 (app router) + Tailwind + shadcn/ui + SWR | Standard SPA pattern; SWR's polling is enough (no WebSocket complexity yet) |
| API | FastAPI + Pydantic v2 + SQLAlchemy 2.0 (asyncio) + asyncpg | Async-native end-to-end; great types |
| Background jobs | arq (Redis-backed) | Already in stack for campaigns; reused for broadcasts |
| WhatsApp | **Baileys** `@whiskeysockets/baileys ^6.7.13` (Node) | Multi-device protocol; lighter than `whatsapp-web.js` (no headless browser) |
| Migrations | alembic with autogenerate + `compare_type=True` | Standard |
| LLM | OpenAI (`gpt-4o-mini`) via `chat.completions.parse` | Used for query gen, scoring, prompt-based filtering, auto-tagging, validity-prompted tagging |
| Search | Serper.dev | Google SERPs |
| Scrape | Firecrawl + asyncpraw (Reddit native) | URL → markdown |
| HTTP client (server) | httpx | All outbound calls |
| QR encoding | `qrcode` npm | Sidecar emits data URLs |

OpenAI lazy singleton: `apps/backend/src/clients/openai_client.py:11` (`get_openai_client`).

---

## 4. Repo layout

```
/                                     repo root
├── docker-compose.yml                 6 services, 2 volumes
├── ARCHITECTURE.md                    THIS FILE
├── README.md                          (legacy intro)
├── plan.md                            scratch notes
├── apps/
│   ├── backend/                       FastAPI + arq worker (one image, two commands)
│   │   ├── Dockerfile
│   │   ├── pyproject.toml             deps: fastapi, sqlalchemy, asyncpg, arq, openai, httpx, ...
│   │   ├── alembic.ini, alembic/      schema migrations
│   │   └── src/
│   │       ├── main.py                ALL HTTP endpoints (no router split yet)
│   │       ├── worker.py              arq WorkerSettings; registers run_campaign + run_broadcast
│   │       ├── core/
│   │       │   └── config.py          pydantic Settings; reads .env + container env
│   │       ├── db/
│   │       │   ├── base.py            declarative Base
│   │       │   ├── models.py          ALL SQLAlchemy models (single file)
│   │       │   └── session.py         async engine + SessionLocal + get_session FastAPI dep
│   │       ├── clients/
│   │       │   ├── openai_client.py   gpt-4o-mini async client (lazy)
│   │       │   ├── serper_client.py
│   │       │   ├── firecrawl_client.py
│   │       │   ├── reddit_client.py   asyncpraw wrapper
│   │       │   └── wa_sidecar_client.py   sidecar HTTP client + custom errors
│   │       └── pipeline/
│   │           ├── run_campaign.py    orchestrator: stages 1→6
│   │           ├── stage1_queries.py … stage6_score.py
│   │           ├── filter_by_prompt.py    LLM yes/no over leads (used by /filter)
│   │           ├── auto_tag.py        LLM tag pass + business/misc reconciler
│   │           ├── whatsapp_validator.py  hardened invite-page fetcher
│   │           ├── wa_enricher.py     orchestrator: validator + DB persistence
│   │           └── broadcaster.py     arq job: throttled bulk send via sidecar
│   ├── frontend/                      Next.js dev with bind-mounted /app
│   │   ├── Dockerfile
│   │   ├── package.json               next 15, react 18, swr, tailwind, lucide-react
│   │   ├── tailwind.config.ts, tsconfig.json
│   │   └── src/
│   │       ├── app/
│   │       │   ├── layout.tsx         shell + top nav (Campaigns / Inbox / Broadcasts / + New)
│   │       │   ├── page.tsx           home dashboard (NumbersCard + campaigns table)
│   │       │   ├── campaigns/
│   │       │   │   ├── new/page.tsx   create-campaign form
│   │       │   │   └── [id]/page.tsx  per-campaign detail (leads, lifecycle tabs, panels)
│   │       │   ├── inbox/page.tsx     two-pane WA-Web-style inbox
│   │       │   └── broadcasts/
│   │       │       ├── page.tsx       cross-number broadcast list
│   │       │       └── [id]/page.tsx  per-broadcast detail (live polling)
│   │       ├── components/
│   │       │   ├── ui/*               shadcn primitives (button, table, input, …)
│   │       │   ├── status-badge.tsx
│   │       │   ├── numbers-card.tsx       NumbersCard + AddNumberModal
│   │       │   ├── broadcast-modal.tsx    Compose + live progress
│   │       │   ├── categorize-panel.tsx   prompt-filter + saved-category CSVs
│   │       │   └── export-panel.tsx       lead CSV export with filters
│   │       └── lib/
│   │           ├── api.ts             ALL fetch wrappers + types
│   │           └── utils.ts           cn() helper
│   └── wa-sidecar/                    Node service, single-file
│       ├── Dockerfile                 alpine + git + python3 + make + g++ (for libsignal native)
│       ├── package.json               baileys, express, qrcode, pino
│       └── src/index.js               session manager + HTTP API (no router split)
└── plan.md
```

---

## 5. Data model (Postgres)

All in `apps/backend/src/db/models.py`. Migrations in `apps/backend/alembic/versions/`. Read top-to-bottom.

### Lead-finder tables (the original)

```sql
campaigns
  id BIGSERIAL PK
  name VARCHAR(200)
  industries          TEXT[]              ICP industries (e.g. ['AI', 'Marketing'])
  locations           TEXT[]              include geo
  negative_locations  TEXT[]              exclude geo (applied in query gen + scoring)
  platforms           TEXT[]              ['reddit', 'web']  (LinkedIn/X deferred)
  status              VARCHAR(32)         'queued' | 'running' | 'done' | 'budget_exceeded' | 'failed'
  current_stage       VARCHAR(32)?        set per-stage during run, NULL when terminal
  max_credits_serper       INT  default 500
  serper_credits_used      INT  default 0
  max_credits_firecrawl    INT  default 200
  firecrawl_credits_used   INT  default 0
  created_at, completed_at TIMESTAMPTZ

queries                                   LLM-generated search queries
  id BIGSERIAL PK
  campaign_id BIGINT FK CASCADE
  query_text TEXT
  source_platform VARCHAR(32)?
  status VARCHAR(32)                      'pending' | 'searching' | 'searched' | 'failed'

search_results                            from Serper
  id BIGSERIAL PK
  query_id BIGINT FK CASCADE
  url, title, snippet TEXT
  position INT?
  status VARCHAR(32)                      'new' | 'fetched' | 'fetch_failed' | 'extracted'
  fetch_strategy VARCHAR(32)?             'snippet_hit' | 'reddit' | 'web' | 'skip' (set by stage 3)

leads                                     THE PRODUCT
  id BIGSERIAL PK
  invite_id VARCHAR(64)                   "AbCdEf123" — UNIQUE per (campaign_id, invite_id)
  source_url, source_text TEXT?           where we found it + ±200 char context
  detected_geo VARCHAR(8)?, language VARCHAR(8)?    (currently NULL — auto-fill not built)
  relevance, geo_fit, engagement INT?     0–100, set by stage 6
  total_score INT?                        weighted: 0.5*rel + 0.3*geo + 0.2*eng
  status VARCHAR(16)                      MODULE 3 LIFECYCLE — see below
  verified_group_name VARCHAR(200)?       MODULE 3 — set by enricher (real WA group name)
  verified_group_description TEXT?        MODULE 3 — almost always NULL (WA's invite page only
                                                     exposes generic boilerplate to non-members)
  last_validated_at TIMESTAMPTZ?          when enricher last hit WA for this invite
  campaign_id BIGINT FK CASCADE
  first_seen, last_seen, last_scored_at TIMESTAMPTZ

  UNIQUE (campaign_id, invite_id)         cross-campaign de-dup is via (campaign × invite)

url_cache                                 saves Firecrawl credits
  url TEXT PK
  markdown TEXT
  fetched_at TIMESTAMPTZ
```

**Lead status lifecycle** (Module 3+4, defaults to `pending`):
```
pending  →  approved  →  joined  →  contacted  →  archived
   ↘                      ↘
    rejected           (or back to approved/pending via the UI)
```
The frontend HIDES the `pending` tab — those leads live in the `All` view. UI buttons let the user transition forward or backward at every stage. Schema enforces nothing; transitions are user-initiated via `PATCH /campaigns/{cid}/leads/{lid}/status`.

### Tags (Module 4)

```sql
tags
  id BIGSERIAL PK
  campaign_id BIGINT FK CASCADE
  name VARCHAR(40)                normalized lowercase, e.g. 'ai', 'business', 'misc'
  color VARCHAR(16)?              unused yet
  created_at TIMESTAMPTZ
  UNIQUE (campaign_id, name)

lead_tags                          join table (M:N, OR-semantics filtering)
  lead_id BIGINT FK CASCADE
  tag_id  BIGINT FK CASCADE
  created_at TIMESTAMPTZ
  PRIMARY KEY (lead_id, tag_id)
```

Three reserved tag names are auto-managed by `auto_tag.py:_refresh_managed_tags` after every `POST /campaigns/{id}/auto-tag` call. They're **mutually exclusive**:
- **Industry tag** = any tag whose name matches one of `campaign.industries`
- **`business`** = lead has no industry tag AND has at least one tag from `BUSINESS_TAG_NAMES` (a 28-entry frozenset: `business, networking, startups, jobs, saas, marketing, sales, …`)
- **`misc`** = lead has neither

Dead leads (those where `last_validated_at IS NOT NULL AND verified_group_name IS NULL`) are **never tagged**. The enricher also deletes any pre-existing tags from a lead the moment it's confirmed dead.

### WhatsApp linked numbers (Module 7)

```sql
whatsapp_numbers
  id BIGSERIAL PK
  display_name VARCHAR(80)               user-supplied label (e.g. "Bangladesh personal")
  msisdn VARCHAR(32)?                    populated by Baileys after pairing; NULL pre-scan
  session_id VARCHAR(64)                 sidecar's session id; UNIQUE
  status VARCHAR(16)                     pending|qr_pending|connecting|connected|disconnected|logged_out
  last_seen_at TIMESTAMPTZ?
  created_at TIMESTAMPTZ
```

Sidecar's in-memory state is volatile; this table + the on-disk session creds are the durable record.

### Broadcast (Module 5)

```sql
broadcast_jobs
  id BIGSERIAL PK
  number_id BIGINT FK CASCADE → whatsapp_numbers
  body TEXT
  status VARCHAR(16)                     'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  total_targets, sent_count, failed_count INT
  min_delay_seconds, max_delay_seconds INT       3 / 15 default; throttle range
  error TEXT?
  created_at, started_at, completed_at TIMESTAMPTZ

outbound_messages                         one row per send attempt
  id BIGSERIAL PK
  job_id BIGINT FK CASCADE → broadcast_jobs
  jid VARCHAR(80)                         group jid we sent to
  group_subject VARCHAR(200)?             snapshot at enqueue time
  status VARCHAR(16)                      'queued' | 'sent' | 'failed'
  error TEXT?
  attempted_at TIMESTAMPTZ
```

### Shared inbox (Module 6)

```sql
conversations                             one per (number, chat-jid)
  id BIGSERIAL PK
  number_id BIGINT FK CASCADE → whatsapp_numbers
  jid VARCHAR(80)                         <user>@s.whatsapp.net | <lid>@lid | <group>@g.us
  kind VARCHAR(8)                         'dm' | 'group'
  name VARCHAR(200)?                      group subject for groups, contact pushName for DMs
  last_message_at TIMESTAMPTZ?
  last_message_preview TEXT?              for groups: 'Sender: msg' (WA-Web style)
  unread_count INT
  created_at TIMESTAMPTZ
  UNIQUE (number_id, jid)

messages
  id BIGSERIAL PK
  conversation_id BIGINT FK CASCADE
  wa_message_id VARCHAR(64)               Baileys message.key.id  — UNIQUE per conversation
  direction VARCHAR(4)                    'in' | 'out'  (out = we sent it OR user sent from phone)
  sender_jid VARCHAR(80)?                 participant jid in groups, contact in DMs
  sender_name VARCHAR(120)?               WhatsApp pushName of message author
                                           NULL for outbound (we are the sender → 'You' on UI)
  body TEXT                               text only — media rendered as '[image]', '[video]', etc.
  ts TIMESTAMPTZ
  status VARCHAR(16)                      'received' | 'sent' | 'delivered' | 'read' | 'failed'
  UNIQUE (conversation_id, wa_message_id) idempotent inbound webhook
```

The same WhatsApp group seen from two different linked numbers is **two conversation rows** (one per number). The unified inbox displays both.

---

## 6. Migrations (alembic, in order)

`apps/backend/alembic/versions/` — names start with date, then short hash. Apply via `docker exec wp_lead_finder-api-1 alembic upgrade head`.

```
79c298b304d9  initial schema (campaigns, queries, search_results, leads, url_cache)
038343ccfab2  add serper budget columns
0acad4eb3f26  add fetch_strategy on search_results
871a4c887dfc  add firecrawl budget columns
83b0e2a8ac10  add last_seen on leads
1a2515ccb857  add current_stage on campaigns
a9bb663053cf  drop unique-only-invite_id (made unique per campaign+invite)
6a4da1412fab  ★ add status column on leads (Module 3 lifecycle)
3154085e26c7  ★ add tags + lead_tags (Module 4)
886c2df6db35  ★ add WhatsApp enrichment cols on leads (verified_group_name, verified_group_description, last_validated_at)
ea3787350acc  ★ add whatsapp_numbers (Module 7)
8b924529b91b  ★ add broadcast_jobs + outbound_messages (Module 5)
7e40fcbc6b93  ★ add conversations + messages (Module 6)
e4459abc73e9  ★ add sender_name on messages (group sender labels)
```

---

## 7. The five pipelines / workflows

### 7a. Campaign / lead-finder (the original 7 stages)

Driver: `apps/backend/src/pipeline/run_campaign.py:run_campaign`. arq job: `worker.py:run_campaign`.

| # | Stage | Where | Concurrency |
|---|---|---|---|
| 1 | LLM generates ~30 Google queries from the ICP | `stage1_queries.py` | sequential |
| 2 | Serper search per query | `stage2_search.py` | 10 |
| 3 | Snippet pre-filter — invite already in snippet? skip the fetch | `stage3_prefilter.py` | in-memory |
| 4 | Route URLs: Reddit → asyncpraw, others → Firecrawl, social → skip | `stage4*_*.py`, `url_cache` honored | 5 |
| 5 | Regex `chat\.whatsapp\.com/[A-Za-z0-9]+` extract + ±200-char context | `stage5_extract.py` | in-memory |
| 6 | LLM scores `(relevance, geo_fit, engagement)` per lead, batches of 10 | `stage6_score.py` | 5 |
| — | finalize: campaign.status = 'done', completed_at = now() | end of `run_campaign` | — |

Each stage commits to DB and is idempotent: re-enqueuing the same `campaign_id` after a crash resumes from "what's still pending" by querying status columns. Worker memory holds nothing.

Stage 6's prompt now includes `verified_group_name` + `verified_group_description` when the enricher has run, so scoring is grounded in the real group rather than just the scraped snippet.

### 7b. Lead enrichment (Module 3 part 2)

Driver: `apps/backend/src/pipeline/wa_enricher.py:enrich_campaign`.

Trigger: `POST /campaigns/{id}/enrich-whatsapp` (sync; for 45 leads ~13s). Validates each invite by hitting `https://chat.whatsapp.com/{invite_id}` and reading `og:title` / `og:description`. Persists the verified name + description on the lead. Drops tags from leads it confirms dead.

Hardening (`whatsapp_validator.py`):
- Concurrency **3** (was 8 originally — lowered after the user asked for "human-paced")
- **Random jitter 100–500ms** before each fetch
- **Redis cache** (key `wa:invite:v2:<invite_id>`, **7-day TTL**) so repeat enrichment is cheap
- **Block / CAPTCHA detection** — only trips on small (`<5KB`) responses + status 4xx/5xx OR known markers (`captcha`, `challenge-platform`, `just a moment`, `cf-error`, `access denied`, `automated requests`, `unusual traffic`). On block: raises `WhatsAppBlocked` and the run aborts loudly so we don't silently mark good groups invalid.
- **Per-run request budget** (`request_budget`, default 500). Raises `WhatsAppBudgetExceeded` rather than burning through.
- **Optional proxy** via `WHATSAPP_PROXY` env var (HTTP CONNECT). Empty = direct egress. Wired but unused by default.

Important nuance: the description (og:description) is **almost always the localized boilerplate** ("WhatsApp Group Invite", "Convite para grupo do WhatsApp", etc.). We strip anything that's `<50 chars` and contains "whatsapp" as the generic-default heuristic. So in practice `verified_group_description` is mostly NULL — `verified_group_name` is the high-signal channel.

### 7c. Auto-tagging (Module 4)

Driver: `apps/backend/src/pipeline/auto_tag.py:auto_tag_campaign`.

Trigger: `POST /campaigns/{id}/auto-tag?only_untagged=true|false`.

Steps:
1. Fetch eligible leads — scored, alive (`NOT (last_validated_at NOT NULL AND verified_group_name IS NULL)`), and untagged unless `only_untagged=false`.
2. LLM batch-tagging (gpt-4o-mini, structured outputs) — 1–3 short tags per lead, biased toward existing tag names + the campaign's industries. Normalized lowercased, ≤40 chars.
3. Upsert tags + `lead_tags` rows; insert with `ON CONFLICT DO NOTHING` for idempotency.
4. **Reconcile managed pseudo-tags** — `_refresh_managed_tags` runs at the end:
   - Industry membership = leads carrying any tag whose name is in `campaign.industries`
   - Business membership = leads with at least one `BUSINESS_TAG_NAMES` tag AND no industry tag
   - Misc membership = neither
   - Adds/removes `business` and `misc` rows so each lead sits in exactly one of {industry, business, misc}.

LLM input includes `verified_group_name` first (highest signal — the actual group name), then `verified_group_description` if non-null, then the scraped `source_text`. Same pattern is used by `filter_by_prompt.py` (the `/filter` endpoint) and `stage6_score.py`.

### 7d. QR pairing (Module 7)

`api` is a thin façade over the Node sidecar. The full handshake:

```
[browser] POST /numbers {display_name}
   ↓
[api]    insert whatsapp_numbers (status='pending', session_id=uuid4().hex)
         POST sidecar /sessions {session_id, display_name}
              ↓
         [sidecar] makeWASocket + useMultiFileAuthState('/data/sessions/<id>')
                   sock.ev.on('connection.update'):
                     - qr event   → store qrcode.toDataURL(qr) in memory
                     - connection='open' → status='connected', extract msisdn from sock.user.id
                     - connection='close' → if loggedOut: status='logged_out' (delete creds)
                                            else: status='disconnected', auto-reconnect after 2s
              ← returns initial snapshot
   ←     return row + qr_data_url

[browser] polls GET /numbers/{id} every 1.5s
   ↓
[api] proxies GET sidecar /sessions/<session_id>, syncs DB row, returns latest QR
   ... user scans on phone ...
[sidecar] connection='open' → status='connected', persists creds to disk
[api] next poll sees status='connected' + msisdn → DB updated → frontend shows "Connected!"
```

Sidecar startup sweep: `resumeExisting()` walks `/data/sessions/*` and re-instantiates each Baileys socket from its persisted creds. After a `docker compose down/up`, every paired number reconnects on its own — no re-pairing.

Critical Baileys configuration (debugged, don't revert):
- `version` from `fetchLatestBaileysVersion()` (cached at startup) — without this WhatsApp servers reject the handshake with status code **405** (stale Web version)
- `browser: Browsers.macOS('Desktop')` — the canonical pairing identifier; custom strings get rejected
- `printQRInTerminal: false`, `markOnlineOnConnect: false`, `syncFullHistory: false`

### 7e. Broadcast (Module 5)

Driver: `apps/backend/src/pipeline/broadcaster.py:run_broadcast` registered as arq job in `worker.py:run_broadcast`.

```
[browser] POST /numbers/{id}/broadcast {body, targets[], min/max_delay_seconds}
   ↓
[api]
   - validate: max ≥ min, number is connected
   - GET sidecar /sessions/<id>/groups → list of {jid, subject, ...}
     (sanity check: every target jid is a group this number is in; otherwise 400)
   - INSERT broadcast_jobs (status='queued', counts seeded)
   - INSERT N outbound_messages (status='queued', group_subject snapshotted now)
   - app.state.arq_pool.enqueue_job('run_broadcast', job.id)
   ← return BroadcastJob

[arq worker] pops job
   - load job + queued OutboundMessage rows
   - status='running', started_at=now
   - for each target:
       sleep(random.uniform(min, max))      ← throttle
       POST sidecar /sessions/<id>/messages {jid, text}
       UPDATE outbound_messages.status = 'sent' | 'failed'
       UPDATE broadcast_jobs.sent_count / failed_count
     - on NotConnected mid-run: stop the WHOLE run, mark job failed loudly
   - status='completed', completed_at=now

[browser] polls GET /broadcasts/{id} every 1.5s; UI stops polling on terminal status
```

**Idempotent on retry**: only `status='queued'` rows are processed. Worker crash mid-run → next worker picks up where the previous left off without double-sending.

The sidecar's `messages.upsert` event ALSO fires for the outbound message we just sent (because Baileys emits it locally), which means the message **also lands in the inbox** through Module 6's webhook. This is intentional — your sent broadcasts show up in the inbox thread alongside replies.

### 7f. Shared inbox webhook (Module 6)

The sidecar listens on every session for `messages.upsert` events of type `notify` (live messages). For each message it builds:

```json
{
  "session_id": "<which linked number>",
  "wa_message_id": "<Baileys key.id>",
  "chat_jid": "<remoteJid>",
  "chat_name": "<group subject for groups | contact pushName for DMs | null>",
  "kind": "dm" | "group",
  "direction": "in" | "out",         // out = fromMe=true (our send OR phone-side reply)
  "sender_jid": "<participant or remoteJid>",
  "sender_name": "<pushName of message author | null for outbound>",
  "body": "<text or [image]/[video]/[audio]/...>",
  "ts_unix": <int>
}
```

POSTs to `${API_URL}/internal/inbound` with `X-Internal-Token: <INTERNAL_TOKEN>` header.

The api side (`main.py:inbound_message`):
- Auth: `_require_internal_token` dep — 401 if header doesn't match `settings.wa_sidecar_secret`
- 204 + drop silently if `session_id` doesn't match any `whatsapp_numbers` row (sidecar startup race)
- Upsert conversation by `(number_id, jid)`, bumping `last_message_at`, `last_message_preview` (with "Sender: msg" prefix for inbound group messages), and `unread_count` (++ on inbound, no change on outbound)
- Insert `messages` row; on UNIQUE collision (duplicate webhook for the same `wa_message_id`) silently drop with rollback

Group subject resolution: the sidecar caches `sock.groupMetadata(jid)` results in-memory for **5 min** (`GROUP_SUBJECT_CACHE_TTL_MS`). On first message in a new group, it pays one network roundtrip; subsequent messages in that group use the cache. Backfill of stale conv.name is exposed via `POST /conversations/refresh-group-names` (manual, idempotent).

Replies from the UI: `POST /conversations/{conv_id}/messages {body}` calls sidecar `send_message`, eagerly inserts an outbound `messages` row (so the UI sees it instantly), then the sidecar's local `messages.upsert` webhook for the same id collides on UNIQUE and is silently dropped.

@lid handling: WhatsApp issues two user-identity formats. `<phone>@s.whatsapp.net` is real phone; `<lid>@lid` is anonymous (the contact didn't share their phone — modern WA default). Both are recognized as `kind='dm'` by the sidecar's `chatKind()`. The frontend renders `@lid` DMs as **"private contact (no phone shared)"** rather than fabricating a phone number from the lid digits.

---

## 8. API surface (FastAPI, `apps/backend/src/main.py`)

All on `app = FastAPI()`, no router split. Grouped logically below.

### Health
- `GET /health` — db + redis ping

### Campaigns + leads (Modules 1–4)
- `GET /campaigns` — list with denormalized lead counts
- `POST /campaigns` — enqueues `run_campaign`
- `GET /campaigns/{id}` — status + budget snapshot
- `GET /campaigns/{id}/activity` — recent search_results (live feed)
- `GET /campaigns/{id}/leads?status=...&tag_id=N&tag_id=M&only_scored=true` — filter combo
- `GET /campaigns/{id}/leads/status-counts` — per-status counts (drives lifecycle tabs)
- `PATCH /campaigns/{id}/leads/{lead_id}/status` — lifecycle transition
- `DELETE /campaigns/{id}/leads/dead` — bulk-delete dead invites (cascades lead_tags)

### Tags (Module 4)
- `GET /campaigns/{id}/tags` — with usage counts
- `POST /campaigns/{id}/auto-tag?only_untagged=true|false` — LLM tagging + business/misc reconcile
- (No manual create/attach/detach UI yet — auto-tag is the only writer)

### LLM filter + export
- `POST /campaigns/{id}/filter {prompt, status?}` — LLM yes/no over the leads, returns matches
- `POST /campaigns/{id}/export.csv` — flexible filter CSV (statuses[], tag_ids[], min/max score, only_scored, only_valid_invites)

### WhatsApp enrichment (Module 3)
- `POST /campaigns/{id}/enrich-whatsapp?only_unvalidated=true|false&request_budget=500`
- Errors: 400 on budget, 503 on `WhatsAppBlocked`

### WhatsApp numbers (Module 7)
- `GET /numbers` — list with sidecar reconciliation (sync each row's status from sidecar)
- `POST /numbers {display_name}` — create + start session, returns first QR
- `GET /numbers/{id}` — single, with live QR (used by AddNumberModal poll)
- `DELETE /numbers/{id}` — sidecar logout + DB row delete (best-effort)
- `GET /numbers/{id}/groups` — proxy to sidecar (409 if not connected)

### Broadcast (Module 5)
- `POST /numbers/{id}/broadcast {body, targets[], min/max_delay_seconds}` — enqueue
- `GET /numbers/{id}/broadcasts` — per-number history
- `GET /broadcasts` — cross-number history with display_name/msisdn join (drives `/broadcasts` page)
- `GET /broadcasts/{job_id}` — detail with per-message log

### Inbox (Module 6)
- `POST /internal/inbound` — sidecar webhook, 401 unless `X-Internal-Token` matches
- `GET /conversations?number_id=&kind=dm|group` — unified feed
- `GET /conversations/{id}/messages`
- `POST /conversations/{id}/messages {body}` — send reply (calls sidecar)
- `POST /conversations/{id}/read` — UI-side unread reset (no WhatsApp read receipt)
- `POST /conversations/refresh-group-names` — backfill stale conv.name from sidecar group subjects

---

## 9. Sidecar HTTP API (`apps/wa-sidecar/src/index.js`)

Internal-only; the api is the only client.

```
POST   /sessions               { session_id?, display_name? }  → create or resume; returns session view
GET    /sessions                                                → list (in-memory snapshot)
GET    /sessions/:id                                            → { session_id, status, qr_data_url?, msisdn?, display_name }
POST   /sessions/:id/logout                                     → graceful logout + delete creds dir
GET    /sessions/:id/groups                                     → groupFetchAllParticipating projected to {jid, subject, participants_count, announce}
POST   /sessions/:id/messages  { jid, text }                    → sock.sendMessage; returns {message_id, timestamp}
GET    /healthz                                                 → { ok: true }
```

In-memory map: `sessions: Map<sessionId, { sock, status, qr, qr_data_url, msisdn, display_name, last_updated }>`.

Status values mirror the DB: `pending | qr_pending | connecting | connected | disconnected | logged_out`.

---

## 10. Frontend layout

```
/                       Home dashboard
                          - NumbersCard (linked WhatsApp numbers + Add/Broadcast/Delete actions)
                          - Campaigns table

/campaigns/new          Create campaign

/campaigns/[id]         Campaign detail
                          - Pipeline progress card (queries / search results / leads / scored / serper / firecrawl)
                          - Live activity feed (recent search_results)
                          - Leads card with:
                              * Status tabs: All / Approved / Rejected / Joined / Contacted / Archived
                                (Pending HIDDEN — leads default to pending, surface in All)
                              * Tag chips: industry tags pinned (emerald), business pinned (amber),
                                misc pinned (dashed), others alphabetical. Multi-select OR filter.
                              * Per-row LeadActions: forward + back transitions at every stage
                              * Header buttons: Enrich WhatsApp · Run auto-tag · Delete dead · Export…
                          - CategorizePanel (prompt-based LLM filter + saved-category CSVs)

/inbox                  WA-Web-styled two-pane shared inbox
                          - Top filter pills: All / Unread / Groups / DMs
                          - Per-number filter (when ≥2 connected)
                          - Search box (client-side over name + last preview)
                          - Conversation list: avatar (circle for DM, rounded-square for group),
                            sender-prefixed preview for groups, unread badge, "via {number}" line
                          - Thread:
                              * Header subtitle: "▣ GROUP CHAT" or "● DIRECT MESSAGE"
                              * Group bubbles: colored sender label per author (consistent hue)
                                with "in {GroupName}" suffix on first-of-author bubble
                              * Reply box + BanReminder panel + ⌘/Ctrl+Enter to send

/broadcasts             Cross-number history
                          - Filter pills: All / Running / Queued / Completed / Failed
                          - Each row: status pill, body preview, "via {number} · +{phone}",
                            sent/failed counts, throttle, relative time, in-flight progress bar

/broadcasts/[id]        Per-broadcast detail (live polling)
                          - Summary card with progress bar + key timestamps + body block
                          - Per-message log table (group, status, error, attempted-at)
```

State management: **SWR** with explicit refresh intervals. No global store. Each page picks its own polling cadence; SWR handles deduping + revalidation.

API client: `apps/frontend/src/lib/api.ts` — single source of truth for backend types. The Pydantic models in `main.py` and the TS types here are kept manually in sync.

---

## 11. Configuration

### .env (host bind to api + worker via `env_file: .env`)
```
DATABASE_URL=postgresql+asyncpg://app:app@postgres:5432/wp2
REDIS_URL=redis://redis:6379/0

OPENAI_API_KEY=sk-...
SERPER_API_KEY=...
FIRECRAWL_API_KEY=fc-...
REDDIT_CLIENT_ID=...
REDDIT_CLIENT_SECRET=...
REDDIT_USER_AGENT=script:wp2-leadfinder:0.1 (by /u/<your-handle>)

# Optional residential proxy for WhatsApp invite-page validity fetches.
# Empty = direct egress. Format: http://USER:PASS@host:port
WHATSAPP_PROXY=

# Internal webhook auth (sidecar → api). docker-compose injects a dev default
# of "dev-internal-secret-change-me"; override here for any non-dev setup.
WA_SIDECAR_SECRET=dev-internal-secret-change-me
```

`.env` is gitignored. CSV dumps (`ai_groups.csv`, `business_groups.csv`, `gen_csvs.py`) are also gitignored — they're throwaway artifacts.

### Compose-injected env
```
api:           WA_SIDECAR_URL=http://wa-sidecar:3001
               WA_SIDECAR_SECRET=dev-internal-secret-change-me
wa-sidecar:    PORT=3001
               SESSIONS_DIR=/data/sessions
               API_URL=http://api:8000
               INTERNAL_TOKEN=dev-internal-secret-change-me
frontend:      NEXT_PUBLIC_API_URL=http://localhost:8000
```

---

## 12. Operational notes (things that have already been debugged)

- **Baileys `npm install` on Alpine fails ENOENT spawn git** — fixed by adding `git python3 make g++` to the sidecar Dockerfile (libsignal is a `git+ssh` peer dep).
- **WhatsApp returns status 405 on every connection** — fix: use `fetchLatestBaileysVersion()` + `Browsers.macOS('Desktop')`. Don't pass a custom browser string.
- **og:description is always "WhatsApp Group Invite" / localized boilerplate** — strip anything `<50 chars` containing "whatsapp" as generic. In practice `verified_group_description` is mostly NULL.
- **Block detection false-positives** — early version flagged real 200KB invite pages because it matched "blocked" anywhere in the body. Tightened to: 4xx/5xx OR (status=200 AND len < 5000 AND any block marker). Real invite pages render to ≥150KB.
- **DMs from `@lid` were silently dropped** — `chatKind()` only matched `@s.whatsapp.net`. Now also recognizes `@lid` (anonymous identity used by privacy-conscious contacts who haven't shared their phone).
- **Stale conversation.name showing sender pushName as group name** — old behavior was `name_hint = push_name`. Fixed: sidecar passes `chat_name` (group subject from `groupMetadata` for groups, contact pushName for DMs); api updates conv.name when authoritative. `POST /conversations/refresh-group-names` exists for one-shot backfill.
- **Tags on dead leads** — auto_tag now skips leads where `last_validated_at IS NOT NULL AND verified_group_name IS NULL`; enricher deletes their existing tags in the same txn.
- **Host-IDE TypeScript diagnostics for the frontend are noisy** — `node_modules` lives in the container (anonymous Docker volume preserves it through bind-mount). The host TS server can't see `react/swr/lucide-react/next` and complains. Compile errors are real only if the **container's Next.js dev server** logs them. Always check `docker logs wp_lead_finder-frontend-1` for the truth.

---

## 13. Throttling + ban-prevention guardrails (Module 5/6 ban posture)

Wired into the broadcaster:
- Default delay window: **3–15s random jitter** between sends (configurable per job)
- Per-job request budget enforced at enqueue time (validates targets up front)
- Hard-stops on mid-run sidecar `NotConnected` (no half-runs that look like a burst)

Wired into the WhatsApp validator:
- Concurrency 3, 100–500ms jitter, 7-day cache, 500-fetch budget per run, optional residential proxy
- Block-page detection that aborts loudly rather than marking groups invalid

User-facing reminder: `BanReminder` component embedded in two surfaces — the broadcast modal (above the Start button) and the inbox reply box. Bullet list reminds the user to vary text, keep delays high, skip admins-only groups, and stay under ~80–100 outbound msgs/number/day on a fresh number.

**The failure mode we explicitly protect against**: number-level bans. Meta's anti-spam ML watches per-account behavior, not IP. A residential proxy (`WHATSAPP_PROXY`) helps for invite-page scraping (the validator) but does **not** hide messaging activity from Meta — every Baileys session is a tracked "linked device" on the user's account.

---

## 14. Realistic timing

| Operation | Time |
|---|---|
| Campaign run (30 queries, ~1000 URLs) | 5–8 min |
| WhatsApp enrichment (45 leads, cold cache) | ~13s |
| WhatsApp enrichment (warm cache) | sub-second |
| Auto-tag pass (45 leads, untagged) | 5–10s |
| Filter-by-prompt over 45 leads | 5–15s |
| Broadcast to 10 groups @ default 3–15s throttle | ~80–150s |
| QR pairing (user-side) | as fast as user scans |

---

## 15. Known gaps (deliberate, can ship later)

- **Auth (Module 8)**. Single-user assumed. Plan: `users` (email, password_hash, role admin|member), JWT in `Authorization: Bearer`, gate every campaign/numbers/conversations route. ~1–2 days.
- **Real-time inbox/broadcast** — currently SWR-polled at 1.5–8s. WebSocket/SSE would feel snappier; not currently a usability problem.
- **Inbox history backfill** — only `notify` events captured. To populate older messages we'd hook Baileys' `chats.upsert` history-sync events on initial pair.
- **Inbox media preview** — images / videos / docs are rendered as `[image]` etc. placeholders. Adding real previews means streaming media bytes from sidecar through api (Baileys exposes `downloadMediaMessage`).
- **Send receipts** (✓ / ✓✓ / ✓✓ blue) — Baileys emits `messages.update` events with status. Easy add.
- **Cancel / pause running broadcast** — needs a `cancel_requested` flag on the job + worker check between sends.
- **Re-run failed-only** — re-enqueue only the `status='failed'` rows of an existing job. Schema supports it; wiring is half a session.
- **Lead → conversation mapping** — knowing that "this DM from +1234… is a reply to broadcast B which targeted lead L". Closes the loop on lifecycle. Needs invite_id → group_jid mapping populated when the user joins a group via a known invite.
- **Country-code routing** — match a number's CC to the group's CC for joining (per the `BUSINESS_TAG_NAMES` style — small data table, not yet wired).
- **WhatsApp-Web-style media messages, voice, etc.** — complex; out of MVP.
- **@lid → phone deanonymization** — not generally possible (it's a WA privacy feature). `sock.onWhatsApp(jid)` can probe phone-form JIDs; for `@lid`-only contacts there's no resolution.

---

## 16. How to operate (dev)

```
# from repo root
docker compose up -d --build       # first time, ~3–5 min for sidecar npm install
docker compose ps                  # all 6 services should be healthy

# alembic
docker exec wp_lead_finder-api-1 alembic upgrade head
docker exec wp_lead_finder-api-1 alembic revision --autogenerate -m "<msg>"

# reload backend (hot-reload runs by default via uvicorn --reload)
# reload sidecar after code change
docker compose build wa-sidecar && docker compose up -d wa-sidecar

# view logs
docker logs -f wp_lead_finder-api-1
docker logs -f wp_lead_finder-wa-sidecar-1
docker logs -f wp_lead_finder-worker-1

# psql
docker exec -it wp_lead_finder-postgres-1 psql -U app -d wp2
```

Browser at `http://localhost:3000`. API at `http://localhost:8000`. Sidecar (`3001`) is internal-only; not exposed to host.

---

## 17. Conventions worth preserving

- **Database is the source of truth** for everything except WhatsApp WS state (sidecar) and the validity cache (Redis, derivable). Worker memory holds nothing across stages.
- **Each pipeline stage / worker job is idempotent.** Retries are safe.
- **No CSRF / no auth between containers.** Internal Docker network is the trust boundary. Public-side: only ports 3000 (browser → frontend) and 8000 (browser → api) are exposed.
- **One `models.py`, one `main.py`** — no router/files split yet. Cheap to navigate while the code is small.
- **No CSS modules / styled-components** — Tailwind utility classes only. Distinct semantic colors: emerald = good/connected/DM, blue = group, amber = caution/business, dashed = misc/disabled, destructive = failed/dead.
- **All LLM prompts use OpenAI structured outputs** (`chat.completions.parse` + a Pydantic response schema). Never raw JSON parsing.
- **Migrations always autogenerated** (`compare_type=True`), then committed verbatim unless we need a manual data backfill.
- **Branch naming** — feature branches like `feature/wa-enrichment-and-lifecycle`. Main stays clean.
- **Commit messages are detailed** — multi-paragraph, list every meaningful change. They're the history a future agent reads.
