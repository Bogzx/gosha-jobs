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
| 🎯 **CV-matched feed** | Every posting is ranked against *your* CV on meaning (sentence embeddings) and on the skills it names (TF-IDF), with seniority taken into account: a student sees internships before Senior roles. No keyword setup needed. "Matches your python, react, docker" tells you why. |
| 🔭 **8 job boards, one feed** | Indeed, LinkedIn, Glassdoor (via [JobSpy](https://github.com/speedyapply/JobSpy)) plus native adapters for **eJobs.ro**, **BestJobs.ro**, **Hipo.ro**, **RemoteOK** and **[Jobicy](https://jobicy.com)** (remote jobs open to Europe). Cross-board duplicates collapse into one card. |
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
        A2[eJobs / BestJobs / Hipo / RemoteOK / Jobicy]
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

**The matching engine:** jobs and CVs are embedded with `all-mpnet-base-v2` (sentence-transformers). Your feed ranks recent jobs on two signals against your CV: the embedding cosine and TF-IDF keyword overlap, each standardised over the candidate set, minus a seniority penalty ([`gosha/seniority.py`](gosha/seniority.py), the same level detector as the Discord filter). Your 👍/👎 history nudges the CV vector (a Rocchio update), and the feed shows what your active searches would take on location and level ("Show all" lifts that). At this scale numpy brute force beats a vector database — embeddings live as `float32` bytes in regular columns. Discord DMs first apply the search's own filters (place, level, age, excluded words; an intern-only search gets training positions only), then score what is left. Searches without a CV, which is every search today, use the DM score: the search's cosine minus a quarter of the posting's mean cosine to a fixed panel of 40 everyday searches ([`gosha/dm_panel.txt`](gosha/dm_panel.txt)), so generic ads that sit close to any query stop riding along. With a CV, the search's query is blended with the CV vector instead.

**How well it ranks — measured, not assumed:** [docs/EVAL.md](docs/EVAL.md) scores the ranking on 202 real postings × 14 sample CVs and on an independent held-out set (261 × 10). The shipped ranking reaches nDCG@10 0.759 on the main set, against 0.549 for the old pure-embedding feed, and 0.604 against 0.535 on the held-out set. The held-out gain is the honest one: the ranking was designed on the main set, and the held-out interval (−0.02 to +0.15) includes zero. Once an active search narrows the feed to the user's level, the gain is within noise. The multilingual model once planned as an upgrade is worse, and 👍/👎 feedback helps modestly. For DMs, the eval runs searches the way users do (the product's filters first, no CV). The DM score was chosen by a rule fixed in advance. Its gain over plain cosine is small and mostly comes from sending fewer postings (2026-10-16: P@10 0.825 → 0.855, recall falls, no search starved). Since 2026-10-17 an unmarked posting goes to a junior search only on positive evidence that beginners can apply. The rule fails closed: on the held-out set, mid/senior postings reaching junior DM searches fall from 29 to 4, and in a production audit wrong candidates fall from 17 to 4. The cost is recall on postings that state no level. CI re-runs both evals on both sets for every ranking change.

## Tech stack

**Backend** · Python 3.11, FastAPI, SQLAlchemy 2 (async), discord.py, APScheduler, sentence-transformers, httpx
**Frontend** · React 18, TypeScript, Vite, Tailwind CSS v4, TanStack Query, Recharts
**Infra** · PostgreSQL 16, Caddy, Docker Compose, GitHub Actions (CI + auto-deploy on push to `main`)
**Tests** · 650+ backend (pytest, SQLite with foreign keys enforced, plus opt-in Postgres tests) + frontend (vitest), scraper adapters tested on live-recorded fixtures, a daily live smoke of every board (`.github/workflows/scraper-smoke.yml`; LinkedIn and Glassdoor advisory, since they throttle GitHub's IPs), and an offline ranking-quality eval that CI re-runs when ranking code changes ([docs/EVAL.md](docs/EVAL.md))

## Self-hosting

Prerequisites: Docker + Compose, a [Discord application](https://discord.com/developers/applications) (bot token + OAuth2 credentials), and optionally a DeepSeek, OpenRouter or Gemini API key for cover letters.

```bash
git clone https://github.com/Bogzx/gosha-jobs.git && cd gosha-jobs

cp .env.example .env        # fill in: DISCORD_TOKEN, SESSION_SECRET,
nano .env                   # DISCORD_CLIENT_ID/SECRET, POSTGRES_PASSWORD, ...

# The containers run as uid 1000; Docker would create these root-owned.
mkdir -p data keys && sudo chown 1000:1000 data keys

docker compose -f docker-compose.prod.yml up -d --build
```

In the Discord developer portal, add the OAuth2 redirect URI:
`https://<your-domain>/api/v1/auth/discord/callback`

<details>
<summary><b>Running behind an existing reverse proxy</b></summary>

If something else already owns ports 80/443 on your server, bind GOSHA to localhost, tell its Caddy to trust that proxy for the client IP, and point your proxy at it:

```env
CADDY_SITE=:80
CADDY_HTTP_BIND=127.0.0.1:8090
CADDY_HTTPS_BIND=127.0.0.1:8443
CADDY_EXTRA_TRUSTED_PROXIES=private_ranges
CADDY_CLIENT_IP_HEADER=X-Forwarded-For
```

The front proxy must then name the client in `X-Forwarded-For` itself, overwriting whatever the request carried. Rate limits key on that address, so a proxy that passes a client-supplied header through lets anyone pick their own bucket. For a Caddy behind Cloudflare, use [`deploy/host-caddy.snippet`](deploy/host-caddy.snippet): it believes `CF-Connecting-IP` only when the TCP peer is in Cloudflare's published ranges, and names everyone else by their own address. Without Cloudflare, a plain `reverse_proxy localhost:8090 { header_up X-Forwarded-For {remote_host} }` does the same. [`scripts/check_client_ip.sh`](scripts/check_client_ip.sh) proves both setups against forged headers, and CI runs it.

Facing Cloudflare directly (no host proxy), leave both variables unset: GOSHA's Caddy trusts Cloudflare's ranges and `CF-Connecting-IP` out of the box.
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

**Without proxies, JobSpy is off unless you allow direct scraping.** By default the JobSpy path (Indeed, LinkedIn, Glassdoor) needs at least one healthy tunnel; with none it logs a warning and skips the search ([`gosha/scraper.py`](gosha/scraper.py)), because a single IP scraping LinkedIn gets blocked within a day or two. `SCRAPE_DIRECT=true` lets JobSpy also use the server's own IP: it is shuffled together with the live tunnels, and with no tunnel alive scraping continues directly. The five native adapters (eJobs, BestJobs, Hipo, RemoteOK, Jobicy) call the boards' own endpoints and need no proxies at all, so a proxy-less install still produces a useful Romanian feed. Glassdoor needs python-jobspy 1.3+ (Glassdoor retired the location endpoint older releases used, and every Glassdoor search returned nothing); it throttles datacenter IPs, so the daily smoke probes it as advisory.
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
├── scrapers/      # one adapter per job board (+ scrape_health.py: yield alerts)
├── bot.py         # Discord adapter (slash commands)
├── pipeline.py    # scrape → match → deliver cycle
├── recommend.py   # feed ranking: embedding + keyword + seniority, search scope
└── llm.py         # DeepSeek / OpenRouter / Gemini provider port
web/               # React SPA (Vite + Tailwind)
tests/             # pytest suite (650+ tests)
eval/              # labelled postings + sample CVs for the ranking eval
```

## License

[GNU AGPL-3.0-only](LICENSE).

You can run it, fork it, and self-host it. The Affero clause is the point: if you run a **modified** version as a network service, you have to offer your users the modified source. Plain use, private modification, and contributing back are all unrestricted.

## Privacy

GOSHA holds real CVs. [`PRIVACY.md`](PRIVACY.md) is the operator-facing version of what the running service tells users at `/privacy` (served from [`gosha/api/legal.py`](gosha/api/legal.py) so it cannot drift from the code). Users can export everything (`GET /api/v1/account/export`) and erase everything (`DELETE /api/v1/account?confirm=DELETE`).

## Backups

**Scraper-failure alerts** go to the owner as a Discord DM only: a board that stops returning jobs or collapses, a cycle with zero jobs, missing descriptions or embeddings, a crashed scheduled job, a hung bot, and (from GitHub Actions) a site or bot that is down. Set `ALERT_DM_USER_IDS` to your numeric Discord user ID; setup, thresholds and the off switch are in [`docs/ALERTS.md`](docs/ALERTS.md).

**Backups are built and tested, but not switched on in production yet.** Their secrets (restic password, bucket keys) go in a separate `.env.backup` that only the backup container reads, never in `.env`. [`docs/BACKUPS.md`](docs/BACKUPS.md) describes an opt-in `backup` service (`pg_dump` + encrypted restic snapshots of the database and the CV files) and a restore drill that loads the newest snapshot into a scratch Postgres and compares row counts with the live database. [`scripts/backup/drill.sh`](scripts/backup/drill.sh) runs backup → restore → CV decryption end to end on throwaway containers, and CI repeats it on every change to the backup code. An operator still has to point it at an off-site repository and run the restore drill there; until then, treat the data as unbacked-up.

## Credits

Built by [Bogdan Truta](https://bogdantruta.com) for a friend named Gosha — then for everyone.
Scraping via [JobSpy](https://github.com/speedyapply/JobSpy) · matching via [sentence-transformers](https://www.sbert.net/) · letters via [DeepSeek](https://api-docs.deepseek.com/) / [OpenRouter](https://openrouter.ai/) / [Gemini](https://ai.google.dev/).
