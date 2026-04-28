# wp_lead_finder

A WhatsApp group lead-finder pipeline. Define an Ideal Customer Profile (ICP)
— industries, target locations, exclusion locations — and the pipeline:

1. Asks an LLM for ~30–80 Google search queries
2. Runs them through Serper
3. Tags each result by fetch strategy (snippet hit / Reddit / web / skip)
4. Fetches Reddit URLs via asyncpraw and web URLs via Firecrawl
5. Regex-extracts every `chat.whatsapp.com/<invite_id>` it sees, with ±200 chars
   of surrounding context
6. Scores each lead against the ICP via OpenAI structured output (relevance,
   geo-fit, engagement, weighted total)

Backend: FastAPI + arq + Postgres + Redis. Frontend: Next.js 15 + Tailwind +
shadcn/ui. Everything runs in Docker.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design and [plan.md](plan.md)
for the 10-step build plan that produced it.

---

## Quick start

You'll need:

- **Docker Desktop** (with ≥ 4 GB RAM allocated)
- API keys: **OpenAI**, **Serper**, **Firecrawl**, **Reddit** (script app)

```bash
git clone https://github.com/FahimShahryer/wp_lead_finder.git
cd wp_lead_finder
cp .env.example .env
# Edit .env and paste in your own API keys
docker compose up -d --build
```

First build takes ~5–10 min. Then:

- **Dashboard**: http://localhost:3000
- **API health**: http://localhost:8000/health
- **API docs**: http://localhost:8000/docs

## Where keys come from

| Service | Where to get it | Free tier |
|---|---|---|
| `OPENAI_API_KEY` | platform.openai.com | ~$5 trial then prepaid (~$0.05/campaign) |
| `SERPER_API_KEY` | serper.dev | 2,500 free searches |
| `FIRECRAWL_API_KEY` | firecrawl.dev | 500 free credits |
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | reddit.com/prefs/apps (script app) | Free |
| `REDDIT_USER_AGENT` | Format: `script:wp_lead_finder:0.1 (by /u/<username>)` | — |

A first end-to-end campaign uses ~30 Serper credits + ~50 Firecrawl credits
and a few cents of OpenAI tokens — well within free tiers.

## Tests

```bash
docker compose exec api pytest tests/ -v
```

70+ tests covering each pipeline stage. Reddit-dependent tests skip cleanly
if Reddit's API is intermittently rate-limiting the runner.
