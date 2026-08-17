<div align="center">

# GOSHA

### The job feed that reads your CV.

**[gosha.bogdantruta.com](https://gosha.bogdantruta.com)** · built for CS students in Romania & beyond

[![CI](https://github.com/Bogzx/gosha-jobs/actions/workflows/ci.yml/badge.svg)](https://github.com/Bogzx/gosha-jobs/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11+-0e9f5b)](https://www.python.org/)
[![React](https://img.shields.io/badge/react-18-0e9f5b)](https://react.dev/)
[![Made by](https://img.shields.io/badge/made%20by-Bogdan%20Truta-123524)](https://bogdantruta.com)

<img src="docs/screenshots/landing.png" alt="GOSHA landing page" width="800" />

</div>

---

The best roles get 200 applicants in the first 48 hours. GOSHA makes sure you're early, every time:

1. **Sign in with Discord** — one click, no forms.
2. **Drop in your CV** — the feed reads it and ranks every fresh posting by how well it actually fits you.
3. **Jobs find you** — new matches land in your Discord DMs; apply, track, and generate cover letters in one place.

<div align="center">
<img src="docs/screenshots/feed-desktop.png" alt="Personalized feed with match scores" width="800" />
</div>

## Features

|  |  |
|---|---|
| 🎯 **CV-matched feed** | Every posting gets a match score from sentence-embedding similarity against *your* CV — no keyword setup needed. "Matches your python, react, docker" tells you why. |
| 🔭 **7 job boards, one feed** | Indeed, LinkedIn, Glassdoor (via [JobSpy](https://github.com/speedyapply/JobSpy)) plus native adapters for **eJobs.ro**, **BestJobs.ro**, **Hipo.ro**, and **RemoteOK**. Cross-board duplicates collapse into one card. |
| 📬 **Discord alerts** | The bot scrapes around the clock and DMs you fresh matches. Saved searches are managed on the web — zero slash-command setup (slash commands still work too). |
| ✅ **Self-filling tracker** | Click **Apply** and the job is already in your tracker. Move it through applied → screen → interview → offer, with notes. |
| ✍️ **AI cover letters** | One click per job: your CV + the posting → a tailored letter via DeepSeek (OpenRouter) or Gemini. Cached 30 days. |
| 👍 **Feedback learning** | Thumbs up/down tune your personal ranking — more of what you like, less of what you don't. |
| 🧹 **Fresh by construction** | Daily dead-link checks expire filled positions; real posted-dates from the boards. |
| 📊 **Built-in analytics** | Privacy-friendly homegrown trackers: DAU, signups, applies — no third-party scripts, no extra cookies. |

<div align="center">
<table>
<tr>
<td align="center"><img src="docs/screenshots/feed-mobile.png" alt="Mobile feed" width="240" /><br/><sub>Mobile feed</sub></td>
<td align="center"><img src="docs/screenshots/job-detail-mobile.png" alt="Job detail with match reasons" width="240" /><br/><sub>Match reasons + one-tap apply</sub></td>
<td align="center"><img src="docs/screenshots/tracker.png" alt="Application tracker" width="320" /><br/><sub>Self-filling tracker</sub></td>
</tr>
</table>
</div>

## How it works

```mermaid
flowchart LR
    subgraph sources["Job boards"]
        A1[Indeed / LinkedIn / Glassdoor]
        A2[eJobs / BestJobs / Hipo / RemoteOK]
    end

    subgraph pipeline["Bot process (hourly cycle)"]
        S[Scrape] --> E[Embed + dedup]
        E --> M[Match vs searches<br/>+ CV similarity]
        M --> D[Deliver]
    end

    subgraph storage["PostgreSQL"]
        DB[(jobs · users · searches<br/>applications · events)]
    end

    A1 --> S
    A2 --> S
    D -->|DM embeds + buttons| Discord[Discord users]
    pipeline <--> DB

    subgraph web["Web platform"]
        SPA[React SPA] --> API[FastAPI /api/v1]
    end
    API <--> DB
    API -->|outbox table| pipeline
    User((You)) --> SPA
    User --> Discord
```

Three processes share one database — the bot (Discord + scheduler), the API (FastAPI for the SPA), and Caddy (static SPA + TLS). The web app never talks to the bot directly: the database, including an `outbox` table for DM requests, is the only contract. Full layering details in [ARCHITECTURE.md](ARCHITECTURE.md).

**The matching engine:** jobs and CVs are embedded with `all-mpnet-base-v2` (sentence-transformers). Your feed is a cosine ranking of recent jobs against your CV vector, nudged by your 👍/👎 history. At this scale numpy brute force beats a vector database — embeddings live as `float32` bytes in regular columns.

## Tech stack

**Backend** · Python 3.11, FastAPI, SQLAlchemy 2 (async), discord.py, APScheduler, sentence-transformers, httpx
**Frontend** · React 18, TypeScript, Vite, Tailwind CSS v4, TanStack Query, Recharts
**Infra** · PostgreSQL 16, Caddy, Docker Compose, GitHub Actions (CI + auto-deploy on push to `main`)
**Tests** · ~300 backend (pytest) + frontend (vitest), scraper adapters tested on live-recorded fixtures

## Self-hosting

Prerequisites: Docker + Compose, a [Discord application](https://discord.com/developers/applications) (bot token + OAuth2 credentials), and optionally a Gemini or OpenRouter API key for cover letters.

```bash
git clone https://github.com/Bogzx/gosha-jobs.git && cd gosha-jobs

cp .env.example .env        # fill in: DISCORD_TOKEN, SESSION_SECRET,
nano .env                   # DISCORD_CLIENT_ID/SECRET, POSTGRES_PASSWORD, ...

docker compose -f docker-compose.prod.yml up -d --build
```

In the Discord developer portal, add the OAuth2 redirect URI:
`https://<your-domain>/api/v1/auth/discord/callback`

<details>
<summary><b>Running behind an existing reverse proxy</b></summary>

If something else already owns ports 80/443 on your server, bind GOSHA to localhost and point your proxy at it:

```env
CADDY_SITE=:80
CADDY_HTTP_BIND=127.0.0.1:8090
CADDY_HTTPS_BIND=127.0.0.1:8443
```

```caddyfile
your-domain.com {
    reverse_proxy localhost:8090
}
```
</details>

<details>
<summary><b>Migrating from an old SQLite install</b></summary>

```bash
docker compose -f docker-compose.prod.yml run --rm \
  -e PYTHONPATH=/app bot \
  python scripts/migrate_sqlite_to_postgres.py \
  sqlite+aiosqlite:///data/jobs.db \
  "postgresql+asyncpg://gosha:$POSTGRES_PASSWORD@postgres:5432/gosha"
```

Users, searches, delivery history, applications, and cover letters all carry over — nobody gets re-spammed with jobs they've already seen.
</details>

<details>
<summary><b>Local development</b></summary>

```bash
python -m venv .venv && .venv/Scripts/activate   # or bin/activate
pip install -r requirements-dev.txt
pytest                                            # backend tests

python scripts/seed_dev.py                        # demo data + dev user

# SESSION_SECRET must be >= 32 chars — it signs session cookies, whose
# payload is just {"uid": N} over a small id space, and admin is an id
# membership test. A guessable secret is a straight path to forging one.
export SESSION_SECRET=$(python -c "import secrets;print(secrets.token_urlsafe(48))")
DEBUG_LOGIN=1 DISCORD_CLIENT_ID=1 DISCORD_CLIENT_SECRET=1 \
  uvicorn gosha.api.app:create_app --factory      # API on :8000

cd web && npm install && npm run dev              # SPA on :5173 (proxies /api)
```

Then open `http://localhost:5173/api/v1/auth/debug-login?uid=1`.

`debug-login` is a source-checkout-only route: [`.dockerignore`](.dockerignore) keeps `gosha/api/debug_login.py` out of the image entirely, so no production deployment can enable it.
</details>

<details>
<summary><b>SSH proxy rotation for scraping</b></summary>

LinkedIn and Glassdoor rate-limit aggressively. Configure up to 9 VPSs in `.env` (`VPS_1_HOST`, `VPS_1_USER`, `VPS_1_KEY`, ...) and the bot opens SOCKS5 tunnels, rotating per scrape with automatic health checks.

**Without proxies you get 4 of the 7 sources.** The JobSpy path (Indeed, LinkedIn, Glassdoor) requires at least one healthy tunnel — with none configured it logs a warning and returns nothing rather than connecting directly ([`gosha/scraper.py`](gosha/scraper.py)), because a direct connection from a single IP gets that IP blocked within a day or two. The four native adapters (eJobs, BestJobs, Hipo, RemoteOK) call the boards' own endpoints directly and work with no proxies at all, so a proxy-less install still produces a useful Romanian feed.
</details>

## Discord commands

The website is the main interface, but the bot speaks fluent slash:

`/quickstart` `/subscribe` `/my_searches` `/edit` `/pause` `/resume` `/unsubscribe` `/scrape_now` `/stats` `/apply` `/applications` `/upload_cv` `/cover_letter` `/my_cv`

## Project layout

```
gosha/
├── domain/        # pure business logic + domain errors
├── services/      # use cases (transactions, rules)
├── api/           # FastAPI HTTP adapter (/api/v1)
├── scrapers/      # one adapter per job board
├── bot.py         # Discord adapter (slash commands)
├── pipeline.py    # scrape → match → deliver cycle
├── recommend.py   # CV-similarity feed ranking
└── llm.py         # OpenRouter / Gemini provider port
web/               # React SPA (Vite + Tailwind)
tests/             # pytest suite (~300 tests)
```

## License

[GNU AGPL-3.0-only](LICENSE).

You can run it, fork it, and self-host it. The Affero clause is the point: if you run a **modified** version as a network service, you have to offer your users the modified source. Plain use, private modification, and contributing back are all unrestricted.

## Privacy

GOSHA holds real CVs. [`PRIVACY.md`](PRIVACY.md) is the operator-facing version of what the running service tells users at `/privacy` (served from [`gosha/api/legal.py`](gosha/api/legal.py) so it cannot drift from the code). Users can export everything (`GET /api/v1/account/export`) and erase everything (`DELETE /api/v1/account?confirm=DELETE`).

## Backups

**There is no backup system running yet.** [`docs/BACKUPS.md`](docs/BACKUPS.md) contains a ready-to-use `backup` service for `docker-compose.prod.yml` plus the setup steps — it is written but **unverified**, and nobody should consider the data safe until a restore has actually been tested. This is the single highest-value thing an operator of this repo can do.

## Credits

Built by [Bogdan Truta](https://bogdantruta.com) for a friend named Gosha — then for everyone.
Scraping via [JobSpy](https://github.com/speedyapply/JobSpy) · matching via [sentence-transformers](https://www.sbert.net/) · letters via [OpenRouter](https://openrouter.ai/) / [Gemini](https://ai.google.dev/).
