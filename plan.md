# Build Plan — WhatsApp Group Lead-Finder

10 sequential steps. Each step has a **test gate** that must pass before moving on. Backend-only; frontend deferred.

---

## Working rules (apply to every step)

- **Everything runs in Docker.** No installing Python, Postgres, or Redis on the host. `docker compose up` is the only dev loop.
- **Use context7 MCP before writing code.** For each library named in a step, call `mcp__context7__resolve-library-id` then `mcp__context7__query-docs` to fetch current API. Don't rely on training data — SDKs drift.
- **No step ships without its test gate passing.** Claude Code runs the tests in-container (`docker compose run --rm api pytest tests/stepN/`) and posts the output. Red = stop, fix, re-run. Green = next step.
- **Real keys, not mocks**, for stages that hit external APIs (OpenAI, Serper, Firecrawl, Reddit). Smoke a tiny scope; budget guards prevent overspend.
- **Each stage is idempotent.** Re-running a step on the same campaign must not duplicate rows or burn duplicate credits.

### Repo layout (target)

```
wp2/
├── docker-compose.yml          # postgres, redis, api, worker
├── .env.example
├── apps/
│   └── backend/
│       ├── Dockerfile
│       ├── pyproject.toml      # uv or poetry
│       ├── alembic/
│       ├── src/
│       │   ├── main.py         # FastAPI
│       │   ├── worker.py       # arq
│       │   ├── db/             # models, session
│       │   ├── pipeline/       # stages 1–7
│       │   ├── clients/        # openai, serper, firecrawl, reddit
│       │   └── core/           # config, logging, budget guard
│       └── tests/
│           ├── step1/
│           ├── step2/ ...
```

### Required env (`.env`)

```
DATABASE_URL=postgresql+asyncpg://app:app@postgres:5432/wp2
REDIS_URL=redis://redis:6379/0
OPENAI_API_KEY=sk-...
SERPER_API_KEY=...
FIRECRAWL_API_KEY=fc-...
REDDIT_CLIENT_ID=...
REDDIT_CLIENT_SECRET=...
REDDIT_USER_AGENT=wp2-leadfinder/0.1
```

---

## Step 1 — Docker infrastructure + FastAPI skeleton

**Goal:** `docker compose up` brings Postgres, Redis, FastAPI, and an arq worker online. `/health` returns 200.

**Build**
- `docker-compose.yml` with services: `postgres` (16-alpine), `redis` (7-alpine), `api`, `worker`. Healthchecks on all four.
- `apps/backend/Dockerfile` (Python 3.12-slim, uv for deps).
- `src/main.py` — FastAPI app with `GET /health` returning `{"db": "ok", "redis": "ok"}` after pinging both.
- `src/worker.py` — empty arq worker that boots, connects to Redis, logs "ready".
- `.env.example`.

**Context7 lookups**
- `fastapi` (latest patterns, lifespan, dependency injection)
- `uvicorn` (production launch flags)
- `arq` (worker boot, settings class)
- `asyncpg` + `redis-py` (async clients)

**Test gate**
- `docker compose up -d` → all four services healthy.
- `curl localhost:8000/health` → `200 {"db":"ok","redis":"ok"}`.
- `docker compose logs worker` shows arq "ready" line.

**Done when** healthcheck-green compose stack and a smoke pytest hits `/health` over the network.

---

## Step 2 — Database schema + Alembic migrations

**Goal:** All tables from `ARCHITECTURE.md §4` exist with correct constraints. Async SQLAlchemy session works end-to-end.

**Build**
- SQLAlchemy 2.0 async models: `Campaign`, `Query`, `SearchResult`, `Lead`, `UrlCache`.
- Constraints: `Lead.invite_id` UNIQUE, `UrlCache.url` PRIMARY KEY, FKs with `ON DELETE CASCADE`, indexes on `campaign_id`, `status`.
- Alembic configured for async; one initial migration.
- `db/session.py` — async session factory + FastAPI dependency.

**Context7 lookups**
- `sqlalchemy` (2.0 async, declarative, `Mapped[]`)
- `alembic` (async config, autogenerate)
- `asyncpg`

**Test gate**
- `docker compose run --rm api alembic upgrade head` succeeds; `\d` in psql shows all tables.
- pytest creates a campaign + 2 leads with same `invite_id` → second insert raises IntegrityError (constraint works).
- Round-trip test: insert campaign → fetch by id → relationships hydrate.

**Done when** migration is reversible (`downgrade -1` works) and the constraint test is green.

---

## Step 3 — Stage 1: Query generation (OpenAI)

**Goal:** `generate_queries(campaign) → list[Query]` produces 20–40 well-formed Google queries from an ICP, persists them.

**Build**
- `clients/openai_client.py` — async OpenAI client, exponential backoff on 429/5xx, timeout, structured-output via pydantic.
- `pipeline/stage1_queries.py` — prompt template that takes industries + locations + negative_locations + platforms, returns queries with `site:` and `-exclusion` operators, mixing English + local terms.
- Persists to `queries` table with `status='pending'`.

**Context7 lookups**
- `openai` Python SDK (latest — structured outputs, `response_format` w/ pydantic)
- `tenacity` (retry decorator)

**Test gate**
- pytest with real `OPENAI_API_KEY`: ICP = `{industries:["AI agency owners","Marketing agency owners"], locations:["US","UK","Dubai"], negative_locations:["India","Indian"]}`.
- Manual assertions: ≥20 queries, ≥5 use `site:reddit.com`, ≥3 use `-india` or `-indian`, no duplicates, all under 200 chars.
- Print queries to logs for human eyeball check.

**Done when** queries look like real prospecting queries a human would write, not generic LLM filler.

---

## Step 4 — Stage 2: Serper search

**Goal:** Fan out queries through Serper concurrently, persist `search_results`. Respects semaphore + budget cap.

**Build**
- `clients/serper_client.py` — httpx async, `search(query) → list[Result]`, retry on 429 honoring `Retry-After`.
- `pipeline/stage2_search.py` — `asyncio.gather` over pending queries with `Semaphore(10)`, INSERT results, mark query `status='searched'`.
- Budget guard: stop when campaign's `serper_credits_used >= max_credits_serper`.

**Context7 lookups**
- `httpx` (async client, retry patterns)
- Serper API — fetch via web (no context7 entry); use `https://serper.dev/api` reference docs

**Test gate**
- pytest with real `SERPER_API_KEY`, 5 queries from step 3 → 50 search_results in DB.
- Re-running stage on same campaign: zero duplicate fetches (queries already `searched` are skipped).
- Manually inspect 10 URLs — they should look topical.

**Done when** 5 queries → ≥40 unique URLs in DB and the budget guard halts a synthetic over-budget run.

---

## Step 5 — Stage 3: Snippet pre-filter + URL tiering

**Goal:** Every `search_result` is tagged with a `fetch_strategy` so Stage 4 knows what to do (or skip) without burning credits.

**Build**
- `pipeline/stage3_prefilter.py`:
  - Regex `chat\.whatsapp\.com/[A-Za-z0-9]+` against `snippet` → if hit, set `fetch_strategy='snippet_hit'` and stash matched ids on the row.
  - URL router: `reddit.com` → `reddit`, known-blocked (`youtube.com`, `instagram.com`, `facebook.com`, `linkedin.com`, `*.pdf`, image hosts) → `skip`, else → `web`.
- Add `fetch_strategy` column to `search_results` (migration).

**Context7 lookups**
- `sqlalchemy` (Alembic add-column migration)
- Python `re`, `urllib.parse` (no context7 needed)

**Test gate**
- Pure-unit pytest: 20 fixture cases covering each branch (snippet hit, reddit, web, youtube, pdf, instagram, mixed-case domains).
- Run over real step-4 data → distribution looks sane (e.g., 20–40% snippet_hit, 30–50% reddit, rest web/skip).

**Done when** zero unrouted rows after stage 3 and the unit suite is green.

---

## Step 6 — Stage 4a: Reddit fetcher (asyncpraw)

**Goal:** Fetch Reddit URLs via the official Reddit API (free), store markdown-ish content in `url_cache`.

**Build**
- `clients/reddit_client.py` — asyncpraw client, OAuth via env, semaphore=3.
- `pipeline/stage4a_reddit.py` — for each `fetch_strategy='reddit'` result: parse permalink → fetch submission + top N comments → render to markdown → UPSERT `url_cache`.
- Handle: removed/deleted posts, private subreddits, comment-only URLs.

**Context7 lookups**
- `asyncpraw` (latest — submission/comment fetch, error types)
- Reddit OAuth script-app docs (web fetch)

**Test gate**
- pytest with real Reddit creds: hit 5 known Reddit URLs containing WhatsApp invites → markdown stored, links present.
- Negative case: deleted post → row marked `fetch_failed='deleted'`, doesn't crash.
- Re-run is a no-op (cache hit, no API call — assert via mock spy).

**Done when** 5/5 real URLs cached and the dead-link case is handled cleanly.

---

## Step 7 — Stage 4b: Firecrawl fetcher + url_cache TTL

**Goal:** Fetch non-Reddit web URLs through Firecrawl, cache for 7 days, respect Firecrawl budget cap.

**Build**
- `clients/firecrawl_client.py` — official `firecrawl-py` SDK, `scrape(url, formats=['markdown'])`, semaphore=5.
- `pipeline/stage4b_firecrawl.py` — for each `fetch_strategy='web'` result: cache lookup (TTL 7d) → hit returns cached markdown → miss calls Firecrawl → UPSERT `url_cache`.
- Errors: `site_not_supported` / `paywall` → mark `fetch_failed`, do not retry.
- Budget guard reads `firecrawl_credits_used`; halts campaign cleanly when exceeded.

**Context7 lookups**
- `firecrawl-py` (latest SDK — `scrape` signature, error types, async support)

**Test gate**
- pytest with real `FIRECRAWL_API_KEY`: 3 real URLs → markdown cached.
- Second run on same URLs → 0 Firecrawl calls (assert via spy + credit counter).
- Force budget exceeded → campaign status flips to `budget_exceeded`, remaining URLs untouched.

**Done when** caching demonstrably saves credits and the budget halts gracefully.

---

## Step 8 — Stage 5: Extract + cross-campaign dedup

**Goal:** Pull every `chat.whatsapp.com/<invite_id>` out of cached content + ±200-char context, INSERT...ON CONFLICT into `leads`.

**Build**
- `pipeline/stage5_extract.py`:
  - Regex `chat\.whatsapp\.com/([A-Za-z0-9_-]+)` over each cached markdown.
  - Capture ±200 chars surrounding context for later scoring.
  - INSERT lead with `ON CONFLICT (invite_id) DO UPDATE SET last_seen=now(), source_text=EXCLUDED.source_text` (keep newest context).
  - Heuristic geo + language detection on context (cheap pre-pass before LLM scoring).

**Context7 lookups**
- `sqlalchemy` (PostgreSQL `INSERT...ON CONFLICT` via `dialects.postgresql.insert`)
- `langdetect` or `lingua-py` (language detection)

**Test gate**
- Pure-unit pytest with fixtures: multi-link page, malformed link (`chat.whatsapp.com/`), URL-encoded link, lookalike (`chat.whatsapp.com.evil.com`) — only valid invite_ids extracted.
- Integration: re-run extraction on the same `url_cache` row twice → no duplicate leads (UNIQUE works), `last_seen` advances.
- Same invite_id from Reddit + web source → 1 row, both URLs traceable.

**Done when** dedup is provably correct and zero false positives on the fixture set.

---

## Step 9 — Stage 6: Lead scoring (OpenAI structured output)

**Goal:** For each unscored lead, ask OpenAI for `relevance`, `geo_fit`, `engagement` (0–100) plus a `total_score`. Batched 10 per call.

**Build**
- `pipeline/stage6_score.py`:
  - Pydantic `LeadScore` schema; OpenAI structured output.
  - Batch leads in groups of 10, parallel via `Semaphore(5)`.
  - Negative-location penalty applied in prompt (so `geo_fit→0` for excluded geos).
  - Persist scores; mark lead `scored`.
- Prompt template lives in `prompts/scoring.md` so it's editable without redeploy.

**Context7 lookups**
- `openai` (structured outputs / `response_format` with pydantic, latest)
- `pydantic` v2 schema features

**Test gate**
- pytest with real `OPENAI_API_KEY`, 10 leads from step 8, ICP from step 3.
- Manual inspection: known-fit lead (right industry + right geo) scores >70 total; known-misfit (wrong geo) scores <30 with `geo_fit ≤ 20`.
- Re-running scoring on already-scored leads is a no-op.

**Done when** scoring distribution looks human-defensible on a real sample.

---

## Step 10 — Wire it all: arq worker + FastAPI + budget guards + e2e

**Goal:** A single `POST /campaigns` triggers an arq job that runs stages 1→7, persists everything, exposes status + leads via REST. End-to-end campaign completes in Docker.

**Build**
- `worker.py` — arq task `run_campaign(campaign_id)` chains all 7 stages, each reading "what's still pending" from DB (no in-memory state).
- FastAPI endpoints:
  - `POST /campaigns` — creates campaign, enqueues, returns id.
  - `GET /campaigns/{id}` — returns `{status, stage, progress, leads_count, credits_used}`.
  - `GET /campaigns/{id}/leads?limit=&order_by=total_score` — paginated scored leads.
- Budget guards (Serper, Firecrawl, OpenAI tokens) check between stages and halt with `status='budget_exceeded'`.
- Resumability test: kill worker mid-stage-4 → restart → it picks up exactly where it left off.

**Context7 lookups**
- `arq` (task definition, retries, `on_job_start`/`on_job_end`, cron)
- `fastapi` (background tasks vs. external worker — confirm we use external worker pattern)

**Test gate**
- e2e pytest in Docker: POST campaign with `industries=["AI agency owners"]`, `locations=["US","UK"]`, `negative_locations=["India"]`, low caps (50 queries, 30 fetches).
- Within ~5 min: campaign reaches `status='done'`, ≥10 leads exist with non-null scores.
- Resumability: same test but `docker compose kill worker` after 60s, then `start` again → completes correctly with no duplicate fetches.
- Cost ledger: `credits_used` matches `len(search_results) + len(firecrawl_fetches)` exactly.

**Done when** a fresh `docker compose up` + one POST yields ranked leads with no manual intervention, and the kill-mid-run test passes.

---

## After step 10

Backend is feature-complete and provably resilient. Only then:

- Add a thin Next.js frontend (campaign builder + leads table) — likely 2–3 days.
- Add auth + multi-tenant — only after one happy user has validated lead quality.
- Consider stage-as-job arq pattern (see `ARCHITECTURE.md §12`) when single-job runs start hurting.
