# Privacy

The user-facing version of this lives at
[`/privacy`](https://gosha.bogdantruta.com/privacy) and is rendered from
`GET /api/v1/legal/privacy` (`gosha/api/legal.py`), so it cannot drift from
what the deployment actually does — in particular *which* LLM provider a
given instance sends CV text to, which is an environment variable, not a
copywriting decision.

This file is the operator-facing version: the same facts, plus the parts a
self-hoster is responsible for.

## Controller

GOSHA Jobs, operated by Bogdan Truta (<https://bogdantruta.com>).
Self-hosted instances have their own controller — you.

## What is collected

| Data | Source | Purpose | Legal basis |
|---|---|---|---|
| Discord user id, username, avatar URL | Discord OAuth (`identify` scope), after the user approves it | Sign-in, and DMing job matches | Contract |
| CV text + a numeric embedding of it | The user uploads it | Ranking postings against their skills | Consent, taken at upload |
| Saved searches | The user creates them | Running the searches | Contract |
| Delivered jobs, thumbs up/down | Product use | Personalising the feed | Legitimate interest |
| Tracked applications and notes | The user enters them | The application tracker | Contract |
| Generated cover letters | The user requests them | Retrieval later | Consent |
| Pageviews (path only) | Product use | Basic usage counts | Legitimate interest |

No email address is collected. No advertising identifiers. No cross-site
tracking, and no third-party JavaScript (`script-src 'self'` in the
`Caddyfile`).

## Third parties that receive personal data

**Discord** (Discord Netherlands BV / Discord Inc.) — receives the user's
Discord id and the content of job alerts DMed to them.
<https://discord.com/privacy>

**The configured LLM provider** — OpenRouter (US) or Google Gemini,
selected by `LLM_PROVIDER` / `OPENROUTER_API_KEY` (`gosha/llm.py`).

> When a user presses **Cover letter**, up to **15,000 characters of their
> CV** (`CV_CHARS_TO_LLM` in `gosha/cover_letter.py`) are sent to that
> provider along with the job description.

This is the single largest disclosure in the product. It happens **only**
on that explicit action — uploading a CV alone never sends it anywhere.
The `/privacy` page states this in the user's own words, and the CV upload
form requires a consent tick that names it.

If you self-host, **this is your disclosure to make**: check the terms of
whichever provider you configure, particularly whether they retain or train
on request content.

## Retention

- **CV text and embedding** — until the user deletes the CV or the account.
- **Cover letters** — until the account is deleted. Deleting a CV now also
  deletes every letter generated from it (`gosha/services/cv.py`); it
  previously left them behind indefinitely.
- **Everything else** — until the account is deleted.

There is currently **no automatic retention limit** on jobs, events or
delivery history. Rows accumulate. This is an open item.

## User rights

| Right | How |
|---|---|
| Access / portability (Art. 15, 20) | `GET /api/v1/account/export` — everything we hold, as JSON. Button on the profile page. |
| Erasure (Art. 17) | `DELETE /api/v1/account?confirm=DELETE` — account row, CV file, cover letters, saved searches, delivery history, tracked applications and the event log. Irreversible. Button on the profile page. |
| Rectification | Discord profile fields refresh on each sign-in; the CV can be re-uploaded. |
| Withdraw consent | Delete the CV, or the account. |

Erasure is implemented as explicit statements rather than ORM cascades
(`gosha/services/account.py`) because the `events` table has no foreign
keys and CV text lives on the filesystem — a cascade would silently miss
both.

## Known gaps — stated, not hidden

These are real and currently unfixed. A privacy notice that omits them is
worse than none.

1. **CVs are stored unencrypted** as plain text under `data/cvs/`, on a
   volume mounted read-write into both the `api` and `bot` containers.
   Encryption at rest is not implemented.
2. **No backups exist yet.** See [`docs/BACKUPS.md`](docs/BACKUPS.md). The
   backup service is written but unverified. Until a restore has been
   tested, a disk failure loses everyone's data — which is a privacy
   failure (availability) as much as an operational one.
3. **Third-party assets on the landing page.** `web/index.html` loads
   Google Fonts and `web/src/pages/Landing.tsx` loads Unsplash imagery,
   which exposes visitor IP addresses to Google and Unsplash. They should
   be self-hosted. Until then the CSP allows exactly those two origins and
   nothing else, and the `/privacy` page says so.
4. **No session revocation.** Signing out clears the cookie; a cookie
   already stolen stays valid for its 30-day lifetime.
5. **No retention limits** on the event log or delivery history.

## Self-hosting checklist

If you run your own instance, you are the controller. At minimum:

- Replace the controller name and contact in `gosha/api/legal.py`.
- Check the terms of the LLM provider you configure and update the
  disclosure if they differ.
- Set up backups (`docs/BACKUPS.md`) and test a restore.
- Set a real `SESSION_SECRET` (≥ 32 chars — the API refuses to start
  otherwise) and keep `data/cvs/` off any shared or world-readable mount.
