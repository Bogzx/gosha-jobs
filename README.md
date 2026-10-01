<div align="center">

# GOSHA

### The job feed that reads your CV.

**[gosha.bogdantruta.com](https://gosha.bogdantruta.com)** · **[live demo, no sign-in](https://gosha.bogdantruta.com/demo)** · built for CS students in Romania & beyond

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
<br/><sub>The signed-in feed. No account? <a href="https://gosha.bogdantruta.com/demo"><code>/demo</code></a> runs the same ranking for a sample CV:</sub><br/>
<img src="docs/screenshots/demo.png" alt="Public demo: real postings ranked for a sample CV, with match reasons" width="800" />
</div>

## Features

|  |  |
|---|---|
| 🧪 **Try it without an account** | [`/demo`](https://gosha.bogdantruta.com/demo) ranks today's postings for one of four sample CVs (one written in Romanian) with the same code as the signed-in feed, "why this matched" included. Only a persona id goes to the server; nothing is stored ([`gosha/api/demo.py`](gosha/api/demo.py)). |
| 🎯 **CV-matched feed** | Every posting gets a match score from sentence-embedding similarity against *your* CV — no keyword setup needed. "Matches your python, react, docker" tells you why. |
| 🔭 **7 job boards, one feed** | Indeed, LinkedIn, Glassdoor (via [JobSpy](https://github.com/speedyapply/JobSpy)) plus native adapters for **eJobs.ro**, **BestJobs.ro**, **Hipo.ro**, and **RemoteOK**. Cross-board duplicates collapse into one card. |
| 📬 **Discord alerts** | The bot scrapes around the clock and DMs you fresh matches. Saved searches are managed on the web — zero slash-command setup (slash commands still work too). |
| ✅ **Self-filling tracker** | Click **Apply** and the job is already in your tracker. Move it through applied → screen → interview → offer, with notes. |
| ✍️ **AI cover letters** | One click per job: your CV + the posting → a tailored letter via DeepSeek (direct or through OpenRouter) or Gemini. Cached 30 days. |
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

**The matching engine:** jobs and CVs are embedded with `all-mpnet-base-v2` (sentence-transformers). Your feed is a cosine ranking of recent jobs against your CV vector, nudged by your 👍/👎 history (a Rocchio update). At this scale numpy brute force beats a vector database — embeddings live as `float32` bytes in regular columns. Discord DMs use the same CV vector: each saved search's query is blended with it, so the search decides what is relevant and your CV decides between postings that fit it equally well (users without a CV are matched on the search alone).

**How well it ranks — measured, not assumed:** [docs/EVAL.md](docs/EVAL.md) scores the ranking on 202 real postings against 14 sample CVs. The honest summary: on that set the embeddings do *not* beat a TF-IDF keyword baseline (nDCG@10 0.546 vs 0.626), they cannot see seniority, and the multilingual model once planned as an upgrade is worse still; the 👍/👎 update does help (+0.10 nDCG@10 after one round). The eval set the current Discord thresholds and is re-run in CI on every ranking change.

## Tech stack

**Backend** · Python 3.11, FastAPI, SQLAlchemy 2 (async), discord.py, APScheduler, sentence-transformers, httpx
**Frontend** · React 18, TypeScript, Vite, Tailwind CSS v4, TanStack Query, Recharts
**Infra** · PostgreSQL 16, Caddy, Docker Compose, GitHub Actions (CI + auto-deploy on push to `main`)
**Tests** · 500+ backend (pytest) + frontend (vitest), scraper adapters tested on live-recorded fixtures, a daily live smoke of the boards (`.github/workflows/scraper-smoke.yml`; Glassdoor excluded), and an offline ranking-quality eval that CI re-runs when ranking code changes ([docs/EVAL.md](docs/EVAL.md))

## Self-hosting

Prerequisites: Docker + Compose, a [Discord application](https://discord.com/developers/applications) (bot token + OAuth2 credentials), and optionally a DeepSeek, OpenRouter or Gemini API key for cover letters.

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
pip install -r requirements-dev.txt -c constraints.txt   # pinned versions
pytest                                            # backend tests

export GOSHA_ENV=development   # plaintext CVs allowed locally; production
                               # requires CV_ENCRYPTION_KEY (.env.example)

python scripts/seed_dev.py                        # demo data + dev user
export DATABASE_URL=sqlite+aiosqlite:///data/dev.db  # the DB seed_dev.py wrote

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

**Without proxies, JobSpy is off unless you allow direct scraping.** By default the JobSpy path (Indeed, LinkedIn, Glassdoor) needs at least one healthy tunnel; with none it logs a warning and skips the search ([`gosha/scraper.py`](gosha/scraper.py)), because a single IP scraping LinkedIn gets blocked within a day or two. `SCRAPE_DIRECT=true` lets JobSpy also use the server's own IP: it is shuffled together with the live tunnels, and with no tunnel alive scraping continues directly. The four native adapters (eJobs, BestJobs, Hipo, RemoteOK) call the boards' own endpoints and need no proxies at all, so a proxy-less install still produces a useful Romanian feed. Glassdoor blocks datacenter IPs outright and is not covered by the daily smoke test, so expect it to yield little from a VPS.
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
└── llm.py         # DeepSeek / OpenRouter / Gemini provider port
web/               # React SPA (Vite + Tailwind)
tests/             # pytest suite (500+ tests)
eval/              # labelled postings + sample CVs for the ranking eval
```

## License

[GNU AGPL-3.0-only](LICENSE).

You can run it, fork it, and self-host it. The Affero clause is the point: if you run a **modified** version as a network service, you have to offer your users the modified source. Plain use, private modification, and contributing back are all unrestricted.

## Privacy

GOSHA holds real CVs. [`PRIVACY.md`](PRIVACY.md) is the operator-facing version of what the running service tells users at `/privacy` (served from [`gosha/api/legal.py`](gosha/api/legal.py) so it cannot drift from the code). Users can export everything (`GET /api/v1/account/export`) and erase everything (`DELETE /api/v1/account?confirm=DELETE`).

## Backups

**Backups are built and tested, but not switched on in production yet.** [`docs/BACKUPS.md`](docs/BACKUPS.md) describes an opt-in `backup` service (`pg_dump` + encrypted restic snapshots of the database and the CV files) and a restore drill that loads the newest snapshot into a scratch Postgres and compares row counts with the live database. [`scripts/backup/drill.sh`](scripts/backup/drill.sh) runs backup → restore → CV decryption end to end on throwaway containers, and CI repeats it on every change to the backup code. An operator still has to point it at an off-site repository and run the restore drill there; until then, treat the data as unbacked-up.

## Credits

Built by [Bogdan Truta](https://bogdantruta.com) for a friend named Gosha — then for everyone.
Scraping via [JobSpy](https://github.com/speedyapply/JobSpy) · matching via [sentence-transformers](https://www.sbert.net/) · letters via [DeepSeek](https://api-docs.deepseek.com/) / [OpenRouter](https://openrouter.ai/) / [Gemini](https://ai.google.dev/).
