"""Job-centric pipeline with three decoupled stages.

Stage 1 — SCRAPE:  Fetch from job boards, upsert into Job table.
Stage 2 — MATCH:   For each subscription, score jobs, enqueue deliveries.
Stage 3 — DELIVER: Pull from delivery queue, send via channels (Discord DM, etc.).

Each stage can run independently. The DB is the glue between them.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd
from sqlalchemy import select, update

from gosha.database import get_session
from gosha.dedup import normalize_key
from gosha.filters import (
    filter_dataframe,
    location_matches,
    matches_company_blacklist,
    matches_excluded_keywords,
    matches_experience_level,
    normalize_location,
    title_is_relevant,
)
from gosha.matching import SemanticMatcher
from gosha.models import Job, Subscription, User, UserJob
from gosha.queue import enqueue_deliveries_batch
from gosha.salary import meets_minimum, normalise_range
from gosha.scrape_health import CycleReport, ScrapeHealth
from gosha.scrapers.base import EXPERIENCE_LEVELS

if TYPE_CHECKING:
    from gosha.bot import JobBot
    from gosha.ssh_tunnels import SSHTunnelManager

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Stage 1 — SCRAPE: fetch raw jobs and store in DB
# ═══════════════════════════════════════════════════════════════════════


async def upsert_jobs(df: pd.DataFrame) -> list[Job]:
    """Insert new jobs or update last_seen_at for existing ones.

    Returns the list of Job objects (both new and updated).
    """
    if df.empty:
        return []

    jobs: list[Job] = []
    now = datetime.now(timezone.utc)

    async with get_session() as session:
        # One SELECT for the whole batch instead of one per scraped row.
        # A broad keyword expands into ~19 searches x 30 results across 7
        # sources, so the per-row lookup was hundreds of round trips per
        # cycle against a Postgres in another container.
        urls = [
            str(u)
            for u in (
                df.get("job_url", pd.Series(dtype=object))
                .fillna(
                    df.get("job_url_direct", pd.Series(dtype=object))
                )
                .fillna(df.get("link", pd.Series(dtype=object)))
                .tolist()
            )
            if u and str(u) != "nan"
        ]
        existing_by_url: dict[str, Job] = {}
        if urls:
            found = await session.execute(select(Job).where(Job.url.in_(urls)))
            existing_by_url = {job.url: job for job in found.scalars().all()}

        for _, row in df.iterrows():
            url = str(
                row.get("job_url")
                or row.get("job_url_direct")
                or row.get("link")
                or ""
            )
            if not url or url == "nan" or len(url) > _URL_MAX:
                continue

            # Fitted to the column widths: Postgres rejects an over-long
            # value and rolls back the whole batch, and a BestJobs posting
            # listing 25 cities is a 300-character location.
            title = _fit(_clean(row.get("title"), "Unknown Title"), 512)
            company = _fit(_clean(row.get("company"), "Unknown"), 256)
            location = _fit(_clean(row.get("location"), ""), 256)
            description = _clean(row.get("description"), "")
            source = _fit(_clean(row.get("site"), "unknown"), 64)

            salary_min = _parse_float(row, "min_amount")
            salary_max = _parse_float(row, "max_amount")
            salary_currency = (
                _fit(str(row["currency"]), 16)
                if "currency" in row and pd.notna(row.get("currency"))
                else None
            )
            # JobSpy calls it "interval"; our adapters emit the same key.
            salary_period = (
                _fit(str(row["interval"]), 16)
                if "interval" in row and pd.notna(row.get("interval"))
                else None
            )
            # Normalise once, here, so the filter and the sort can compare
            # eJobs' monthly RON with RemoteOK's annual USD.
            monthly_min, monthly_max = normalise_range(
                salary_min, salary_max, salary_currency, salary_period,
            )
            posted_at = _parse_datetime(row, "date_posted")
            level = row.get("experience_level")
            experience_level = level if level in EXPERIENCE_LEVELS else None

            existing = existing_by_url.get(url)

            if existing:
                existing.last_seen_at = now
                existing.is_active = True
                # The vector embeds title, company and description; when
                # any of them changes (typically a description arriving
                # from a detail page for a job first seen title-only),
                # drop it so the embed step re-encodes the posting.
                if (
                    (title != "Unknown Title" and title != existing.title)
                    or (company != "Unknown" and company != existing.company)
                    or (description and description != existing.description)
                ):
                    existing.embedding = None
                    existing.embedding_model = None
                if title != "Unknown Title":
                    existing.title = title
                if company != "Unknown":
                    existing.company = company
                if location:
                    existing.location = location
                if description:
                    existing.description = description
                if salary_min is not None:
                    existing.salary_min = salary_min
                if salary_max is not None:
                    existing.salary_max = salary_max
                if salary_currency:
                    existing.salary_currency = salary_currency
                if salary_period:
                    existing.salary_period = salary_period
                if monthly_min is not None:
                    existing.salary_monthly_min_ron = monthly_min
                if monthly_max is not None:
                    existing.salary_monthly_max_ron = monthly_max
                if posted_at is not None:
                    existing.posted_at = posted_at
                if experience_level is not None:
                    existing.experience_level = experience_level
                jobs.append(existing)
            else:
                job = Job(
                    url=url,
                    title=title,
                    company=company,
                    location=location,
                    description=description,
                    salary_min=salary_min,
                    salary_max=salary_max,
                    salary_currency=salary_currency,
                    salary_period=salary_period,
                    salary_monthly_min_ron=monthly_min,
                    salary_monthly_max_ron=monthly_max,
                    source=source,
                    first_seen_at=now,
                    last_seen_at=now,
                    posted_at=posted_at,
                    experience_level=experience_level,
                )
                session.add(job)
                jobs.append(job)
                # A batch can contain the same URL twice (two keywords hit
                # the same posting); without this the second occurrence
                # would insert a duplicate and trip the unique constraint.
                existing_by_url[url] = job

        await session.commit()

    # Emit events for new jobs (best-effort)
    try:
        from gosha.events import emit_job_discovered
        for job in jobs:
            if job.id and job.first_seen_at == now:  # Newly created
                await emit_job_discovered(job.id, job.source, job.url)
    except Exception:
        pass

    log.info("Upserted %d jobs into DB", len(jobs))
    return jobs


async def run_scrape_stage(
    tunnel_manager: SSHTunnelManager,
    health: ScrapeHealth | None = None,
) -> list[Job]:
    """Stage 1: scrape all active subscriptions and upsert results.

    Returns all jobs found (new + updated) that passed the relevance
    filter. Per-source yields are folded into `health` (default: the
    process-wide tracker) so a board that has quietly stopped returning
    anything gets alerted on — see gosha/scrape_health.py. Health is judged
    on what each board RETURNED, before the relevance filter: a board that
    answers with 200 postings none of which suit an internship search is
    working; one that answers with nothing is not.
    """
    from gosha.scrape_health import get_scrape_health
    from gosha.scraper import scrape_jobs_raw

    health = health or get_scrape_health()
    attempted: set[str] = set()
    scraped: dict[str, set[str]] = {}

    async with get_session() as session:
        result = await session.execute(
            select(Subscription).where(Subscription.is_active.is_(True))
        )
        all_subs = result.scalars().all()

    if not all_subs:
        # Not a failure — nobody has asked for anything, so no alert.
        log.info("No active subscriptions — nothing to scrape.")
        return []

    # Group by scrape parameters to avoid duplicate scraping
    scrape_combos: dict[tuple, list[str]] = {}  # key -> list of primary keywords (for filtering)
    for sub in all_subs:
        for keyword in sub.keywords:
            for location in sub.locations:
                key = (keyword.lower(), location.lower(), tuple(sorted(sub.boards)), sub.max_age_days)
                scrape_combos.setdefault(key, []).append(keyword)

    all_jobs: list[Job] = []
    for (keyword, location, boards, max_age_days), keywords in scrape_combos.items():
        attempted.update(boards)
        try:
            df = await scrape_jobs_raw(
                tunnel_manager, keyword, location, max_age_days, boards=list(boards),
            )
        except Exception as exc:  # one search must not end the cycle
            log.error("JobSpy search %r in %r failed: %s", keyword, location, exc)
            continue
        if df.empty:
            continue
        _count_scraped(scraped, df)
        df = filter_dataframe(df, keywords)
        all_jobs.extend(await _upsert_safely(df, f"JobSpy {keyword!r} in {location!r}"))

    # Extra sources (Romanian boards + remote feeds): direct APIs, no
    # proxies. Additive for every subscription regardless of its boards
    # list, wherever the source can serve the location.
    all_jobs.extend(await _scrape_extra_sources(scrape_combos, attempted, scraped))

    # Deduplicate by ID
    seen_ids: set[int] = set()
    unique: list[Job] = []
    kept: dict[str, int] = {}
    for j in all_jobs:
        if j.id not in seen_ids:
            seen_ids.add(j.id)
            unique.append(j)
            kept[j.source] = kept.get(j.source, 0) + 1

    health.record_cycle(
        attempted, {source: len(urls) for source, urls in scraped.items()}, kept,
    )

    log.info("Scrape stage complete: %d unique jobs", len(unique))
    return unique


def _count_scraped(scraped: dict[str, set[str]], df: pd.DataFrame) -> None:
    """Fold a raw result frame's URLs into the per-source tally."""
    if "job_url" not in df.columns:
        return
    sites = df["site"] if "site" in df.columns else pd.Series("unknown", index=df.index)
    for site, url in zip(sites, df["job_url"]):
        if isinstance(url, str) and url:
            scraped.setdefault(str(site or "unknown"), set()).add(url)


async def _upsert_safely(df: pd.DataFrame, what: str) -> list[Job]:
    """upsert_jobs that logs instead of raising.

    One batch the database rejects must cost that batch, not the whole
    cycle's matching and delivery for every user.
    """
    if df.empty:
        return []
    try:
        return await upsert_jobs(df)
    except Exception as exc:
        log.error("Storing %d jobs from %s failed: %s", len(df), what, exc)
        return []


# Keyword expansion fans a broad term into up to ~19 searches. eJobs, Hipo,
# RemoteOK and Jobicy answer every term of a combo from one cached listing,
# so in practice only BestJobs makes a request per term.
MAX_EXTRA_TERMS = 20


async def _scrape_extra_sources(
    scrape_combos: dict[tuple, list[str]],
    attempted: set[str] | None = None,
    scraped: dict[str, set[str]] | None = None,
) -> list[Job]:
    """Run the plugin scrapers for every (keyword, location) combo.

    A source is added to `attempted` when it applies to at least one
    combo's location, so a source that returns nothing is still counted —
    otherwise a dead adapter would vanish from the yield accounting instead
    of raising an alert. A source that applies nowhere (RemoteOK when
    nobody searches remote) is not attempted, and so cannot look broken.
    """
    from gosha.filters import expand_keyword, relevance_predicate
    from gosha.scrapers.base import SearchQuery

    try:
        from gosha.scrapers.registry import get_extra_scrapers
        scrapers = get_extra_scrapers()
    except Exception as exc:
        log.warning("Extra scrapers unavailable: %s", exc)
        return []

    collected: list[Job] = []
    for (keyword, location, _boards, max_age_days), keywords in scrape_combos.items():
        terms = expand_keyword(keyword)[:MAX_EXTRA_TERMS]
        relevant = relevance_predicate(keywords)
        for scraper in scrapers:
            probe = SearchQuery(keyword=keyword, location=location, max_age_days=max_age_days)
            if not _applies(scraper, probe):
                continue
            if attempted is not None:
                attempted.add(scraper.name)
            frames: list[pd.DataFrame] = []
            for term in terms:
                query = SearchQuery(
                    keyword=term, location=location, max_age_days=max_age_days,
                    relevant=relevant,
                )
                try:
                    raw_jobs = await scraper.search(query)
                except Exception as exc:  # adapters shouldn't raise; belt & braces
                    log.warning("%s scraper failed for %r: %s", scraper.name, term, exc)
                    raw_jobs = []
                if raw_jobs:
                    frames.append(pd.DataFrame([r.to_record() for r in raw_jobs]))

            if not frames:
                continue
            df = pd.concat(frames, ignore_index=True)
            df = df.drop_duplicates(subset=["job_url"], keep="first")
            if scraped is not None:
                _count_scraped(scraped, df)
            df = filter_dataframe(df, keywords)
            jobs = await _upsert_safely(df, f"{scraper.name} {keyword!r} in {location!r}")
            if jobs:
                collected.extend(jobs)
                log.info(
                    "%s: %d jobs for %r in %r", scraper.name, len(jobs), keyword, location,
                )
    return collected


def _applies(scraper: object, query: object) -> bool:
    applies_to = getattr(scraper, "applies_to", None)
    if applies_to is None:
        return True
    try:
        return bool(applies_to(query))
    except Exception as exc:
        log.warning("%s.applies_to failed: %s", getattr(scraper, "name", scraper), exc)
        return False


# ═══════════════════════════════════════════════════════════════════════
# Stage 2 — MATCH: score jobs against subscriptions, enqueue deliveries
# ═══════════════════════════════════════════════════════════════════════


def job_matches_subscription(job: Job, sub: Subscription) -> bool:
    """Check if a stored Job matches a Subscription's criteria."""
    combined_text = f"{job.title} {job.description or ''}"
    if matches_excluded_keywords(combined_text, sub.excluded_keywords):
        return False
    if matches_company_blacklist(job.company, sub.company_blacklist):
        return False
    # Compared on the normalised monthly-RON figures so a subscription's
    # "salary_min" means the same thing regardless of which board the
    # posting came from.
    if not meets_minimum(
        sub.salary_min, job.salary_monthly_min_ron, job.salary_monthly_max_ron,
    ):
        return False
    if not matches_experience_level(job.title, sub.experience_levels, job.experience_level):
        return False

    # Location matching — job must match at least one subscription location
    if not sub.locations:
        # No location filter → all locations match (user didn't restrict)
        return True

    for loc in sub.locations:
        _search_loc, match_subs = normalize_location(loc)
        if location_matches(job.location, match_subs):
            return True

    # Also include remote jobs when remote_ok is enabled
    if sub.remote_ok:
        _, remote_subs = normalize_location("remote")
        if location_matches(job.location, remote_subs):
            return True

    return False


def _title_relevant_for_any_keyword(
    title: str, keywords: list[str], experience_level: str | None = None,
) -> bool:
    """Check if title is relevant for at least one subscription keyword.

    `experience_level` (the board's label) as the scrape stage's filter
    uses it, so a posting kept there is not dropped here.
    """
    for kw in keywords:
        if title_is_relevant(title, kw, experience_level):
            return True
    return False


def blend_with_user_vector(
    query_emb: np.ndarray, user_vector: np.ndarray | None,
) -> np.ndarray:
    """The subscription query pulled toward the user's CV.

    Discord delivery used to score jobs against the subscription string
    alone ("Job role: software engineer. Location: Cluj"), so the product's
    promise — the job feed that reads your CV — held on the website and
    not in the DMs. The subscription still anchors the query (it is what
    the user asked this search for); the CV decides between postings that
    fit it equally well on paper.

    The midpoint (q + u) / 2, deliberately NOT re-normalised: a job's score
    is then the mean of its cosine to the search and to the CV, which stays
    on the scale the configured threshold was tuned for. Re-normalising
    divides by |q + u| = sqrt(2 + 2 q.u), inflating every score by
    sqrt(2 / (1 + q.u)); a search and a CV in the same field have
    q.u ~ 0.5 under all-mpnet-base-v2 (measured), i.e. +15%, which let
    more jobs through for CV holders instead of better ones.
    Vectors from different models (shape mismatch) are not mixed.
    """
    if user_vector is None:
        return query_emb
    query = np.asarray(query_emb, dtype=np.float32)
    user = np.asarray(user_vector, dtype=np.float32)
    if query.shape != user.shape:
        log.warning(
            "CV vector %s and subscription vector %s differ in shape — "
            "embedded with different models? Matching on the subscription only.",
            user.shape, query.shape,
        )
        return query
    return ((query + user) / 2).astype(np.float32)


async def match_jobs_for_subscription(
    sub: Subscription,
    jobs: list[Job],
    semantic_matcher: SemanticMatcher | None = None,
    user_vector: np.ndarray | None = None,
) -> list[tuple[Job, float]]:
    """Return (job, score) pairs for jobs matching a subscription.

    If a SemanticMatcher is provided and available, uses cosine similarity
    for scoring — against the subscription blended with `user_vector` (the
    user's CV, refined by feedback) when there is one. Otherwise falls back
    to regex with score=1.0.
    """
    # Pre-filter: hard filters that must pass regardless of matching mode
    candidates: list[Job] = []
    for job in jobs:
        if not job_matches_subscription(job, sub):
            continue
        candidates.append(job)

    if not candidates:
        return []

    keywords = sub.keywords

    # Semantic matching path — reuses embeddings stored at scrape time
    # instead of re-encoding the same postings for every subscription.
    if semantic_matcher and semantic_matcher.available:
        # Encoding runs in a thread: on the loop it stalled the Discord
        # gateway (missed heartbeats, "interaction failed").
        query_emb = await asyncio.to_thread(
            semantic_matcher.encode_subscription,
            keywords, sub.locations, sub.experience_levels,
        )
        if query_emb is not None:
            from gosha.embeddings import score_jobs_against_query_async

            # Blended only when the vectors share a space (same shape).
            with_cv = user_vector is not None and np.shape(user_vector) == np.shape(query_emb)
            query_emb = blend_with_user_vector(query_emb, user_vector)
            scores = await score_jobs_against_query_async(query_emb, candidates)
            matches = [
                (job, score)
                for job, score in zip(candidates, scores)
                if semantic_matcher.is_match(score, with_cv=with_cv)
            ]
            # Sort by score descending
            matches.sort(key=lambda x: x[1], reverse=True)
            return matches

    # Regex fallback path
    matches: list[tuple[Job, float]] = []
    for job in candidates:
        if keywords and not _title_relevant_for_any_keyword(
            job.title, keywords, job.experience_level,
        ):
            continue
        matches.append((job, 1.0))
    return matches


async def _cv_vector_for(user: User) -> np.ndarray | None:
    """The user's ranking vector when they have uploaded a CV, else None.

    Users without a CV keep pure subscription matching: a vector built from
    a handful of thumbs-up alone is too thin to override what they asked for.
    """
    from gosha.embeddings import is_current

    if not user.cv_embedding or not is_current(user.cv_embedding_model):
        return None
    try:
        from gosha.recommend import build_user_signal

        return (await build_user_signal(user.id)).vector
    except Exception as exc:
        log.warning("CV vector unavailable for user %d: %s", user.id, exc)
        return None


async def run_match_stage(
    jobs: list[Job],
    semantic_matcher: SemanticMatcher | None = None,
) -> int:
    """Stage 2: match jobs against all active subscriptions and enqueue deliveries.

    Feedback reaches this stage through the user's vector (CV refined by
    👍/👎, gosha/recommend.py), which is blended into each search's query.
    The stored score is that blended cosine, the number the delivery
    threshold was compared with.

    A user gets one delivery per cross-board duplicate group (Job.
    dedup_group_id, gosha/dedup.py), across cycles: the Indeed copy of a
    LinkedIn posting DM'd last week is not a new job.

    Returns total number of new deliveries enqueued.
    """
    async with get_session() as session:
        result = await session.execute(
            select(Subscription, User)
            .join(User)
            .where(Subscription.is_active.is_(True))
        )
        # Discord-notifying searches first: a user gets one delivery per
        # job, recorded against the first search that matched it, and only
        # a notifying search's delivery is DM'd. Website-only searches
        # first meant a job both matched was never DM'd.
        sub_user_pairs = sorted(result.all(), key=lambda pair: not pair[0].notify_discord)

    if not sub_user_pairs:
        return 0

    # user_id -> CV vector refined by feedback (the one the web feed ranks
    # by); None without a CV. Only needed on the semantic path.
    user_vectors: dict[int, np.ndarray | None] = {}

    # Build batch delivery items
    delivery_items: list[dict] = []
    group_of: dict[int, int] = {}
    key_of: dict[int, str | None] = {}

    for sub, user in sub_user_pairs:
        if semantic_matcher is not None and user.id not in user_vectors:
            user_vectors[user.id] = await _cv_vector_for(user)

        matches = await match_jobs_for_subscription(
            sub, jobs, semantic_matcher, user_vectors.get(user.id),
        )
        for job, score in matches:
            group_of[job.id] = job.dedup_group_id or job.id
            key_of[job.id] = normalize_key(job.title or "", job.company or "")
            delivery_items.append({
                "user_id": user.id,
                "job_id": job.id,
                "subscription_id": sub.id,
                "score": score,
            })

    delivery_items = await _one_per_duplicate_group(delivery_items, group_of, key_of)
    if not delivery_items:
        log.info("Match stage: no new matches found.")
        return 0

    created = await enqueue_deliveries_batch(delivery_items)
    log.info("Match stage complete: %d new deliveries enqueued", created)
    return created


async def _one_per_duplicate_group(
    items: list[dict],
    group_of: dict[int, int],
    key_of: dict[int, str | None] | None = None,
) -> list[dict]:
    """Drop items whose duplicate group the user already has a delivery in.

    Two checks, because the group link alone is not durable: dedup only
    groups active postings, so once the copy a user was sent expires, its
    twin on another board is ungrouped again and would look new. The
    posting's identity (gosha.dedup.normalize_key: title and employer) is
    therefore also compared with the user's deliveries of the dedup window
    whose job is no longer active. Active ones need no identity check (the
    group covers them), and leaving them out keeps a second, distinct
    "Software Engineer" opening at a large employer from being dropped.

    Within the batch the first item per (user, group) wins, which is the
    one from the first (Discord-notifying) search that matched.
    """
    from gosha.dedup import WINDOW_DAYS, normalize_key

    key_of = key_of or {}
    groups = set(group_of.values())
    users = {item["user_id"] for item in items}
    taken: set[tuple[int, int]] = set()
    taken_keys: set[tuple[int, str]] = set()
    if groups and users:
        since = datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)
        async with get_session() as session:
            rows = await session.execute(
                select(UserJob.user_id, Job.dedup_group_id, Job.id)
                .join(Job, UserJob.job_id == Job.id)
                .where(
                    UserJob.user_id.in_(users),
                    (Job.dedup_group_id.in_(groups)) | (Job.id.in_(groups)),
                )
            )
            for user_id, group_id, job_id in rows.all():
                taken.add((user_id, group_id or job_id))
            if any(key_of.values()):
                recent = await session.execute(
                    select(UserJob.user_id, Job.title, Job.company)
                    .join(Job, UserJob.job_id == Job.id)
                    .where(
                        UserJob.user_id.in_(users),
                        UserJob.delivered_at >= since,
                        Job.is_active.is_(False),
                    )
                )
                for user_id, title, company in recent.all():
                    key = normalize_key(title or "", company or "")
                    if key:
                        taken_keys.add((user_id, key))

    kept: list[dict] = []
    for item in items:
        group = (item["user_id"], group_of.get(item["job_id"], item["job_id"]))
        key = key_of.get(item["job_id"])
        if group in taken or (key and (item["user_id"], key) in taken_keys):
            continue
        # Within the batch the group decides: active twins were grouped
        # just before matching (assign_dedup_groups).
        taken.add(group)
        kept.append(item)
    if len(kept) < len(items):
        log.info(
            "Match stage: %d matches skipped — already delivered, or a "
            "cross-board duplicate of one", len(items) - len(kept),
        )
    return kept


# ═══════════════════════════════════════════════════════════════════════
# Stage 3 — DELIVER: send notifications to users
# ═══════════════════════════════════════════════════════════════════════

# DMs per user per cycle. A new subscriber's first cycle can match dozens
# of postings; the best ones go out, the rest stay on the website and the
# user is told how many more there are.
MAX_DMS_PER_USER = 10
# A match whose DM keeps failing for transient reasons (Discord 5xx, rate
# limits, a deploy mid-loop) is retried each cycle for this long.
DM_RETRY_WINDOW = timedelta(days=2)


async def deliver_pending(bot: JobBot) -> int:
    """Send every pending Discord DM; return how many were sent.

    "Pending" is a saved search's match the match stage marked pending
    (UserJob.dm_status), that the user has not rated on the website,
    recorded within DM_RETRY_WINDOW. The previous
    version sent the rows recorded since the cycle started, which marked a
    match delivered before any DM went out: one Discord 503 lost it for
    good, and a website 👎 recorded mid-cycle was DM'd as a match.
    """
    from gosha.views import build_job_embed_with_buttons

    cutoff = datetime.now(timezone.utc) - DM_RETRY_WINDOW
    async with get_session() as session:
        result = await session.execute(
            select(UserJob, Job, User, Subscription)
            .join(Job, UserJob.job_id == Job.id)
            .join(User, UserJob.user_id == User.id)
            .join(Subscription, UserJob.subscription_id == Subscription.id)
            .where(
                UserJob.dm_status == UserJob.DM_PENDING,
                UserJob.feedback.is_(None),
                UserJob.delivered_at >= cutoff,
                Job.is_active.is_(True),
            )
            .order_by(UserJob.id.asc())
        )
        rows = result.all()

    by_user: dict[int, list[tuple[UserJob, Job, User, Subscription]]] = {}
    for row in rows:
        by_user.setdefault(row[2].id, []).append(row)

    sent = 0
    closed_dms: list[int] = []  # discord ids whose DMs are closed
    for items in by_user.values():
        statuses: dict[int, str] = {}
        sendable: list[tuple[UserJob, Job, User, Subscription]] = []
        seen_titles: set[tuple[str, str]] = set()
        for uj, job, user, sub in items:
            # Web-managed searches can opt out of Discord notifications;
            # matches still show up on the website.
            if not sub.notify_discord:
                statuses[uj.id] = UserJob.DM_SKIPPED
                continue
            # Same title + company from two boards in one batch: one DM.
            key = ((job.title or "").lower().strip(), (job.company or "").lower().strip())
            if key in seen_titles:
                statuses[uj.id] = UserJob.DM_SKIPPED
                continue
            seen_titles.add(key)
            sendable.append((uj, job, user, sub))

        sendable.sort(key=lambda row: row[0].relevance_score or 0.0, reverse=True)
        for uj, *_rest in sendable[MAX_DMS_PER_USER:]:
            statuses[uj.id] = UserJob.DM_SKIPPED
        overflow = len(sendable) - MAX_DMS_PER_USER

        sent_at: dict[int, datetime] = {}
        for uj, job, user, sub in sendable[:MAX_DMS_PER_USER]:
            kw_str = ", ".join(sub.keywords[:3])[:80]
            loc_str = ", ".join(sub.locations[:2])[:60]
            embed, view = build_job_embed_with_buttons(
                job, uj.id, uj.relevance_score, f"{kw_str} in {loc_str}",
            )
            outcome = await _dm_user(bot, user.discord_user_id, embed, view)
            if outcome == DM_SENT:
                statuses[uj.id] = UserJob.DM_SENT
                sent_at[uj.id] = datetime.now(timezone.utc)
                sent += 1
                try:
                    from gosha.events import emit_job_delivered
                    await emit_job_delivered(job.id, user.id, "discord_dm")
                except Exception:
                    pass
            elif outcome == DM_CLOSED:
                # DMs closed: give up on this user's batch, don't retry.
                for other, *_ in sendable[:MAX_DMS_PER_USER]:
                    statuses.setdefault(other.id, UserJob.DM_FAILED)
                closed_dms.append(user.discord_user_id)
                break
            # DM_RETRY: left pending for the next cycle.

        if overflow > 0 and sent_at:
            site = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
            where = f"<{site}/feed>" if site else "the website"
            await _send_text(
                bot, items[0][2].discord_user_id,
                f"…and **{overflow}** more matches. See them all on {where}.",
            )

        # Recorded per user, so a crash (or a deploy) mid-loop costs at most
        # one user's statuses, not a re-send of everyone's batch.
        await _record_dm_statuses(statuses, sent_at)

    if closed_dms and bot.alert_channel_id:
        try:
            channel = bot.get_channel(bot.alert_channel_id)
            if channel is not None:
                mentions = " ".join(f"<@{uid}>" for uid in dict.fromkeys(closed_dms))
                await channel.send(
                    f"**I found jobs for you but can't deliver them!**\n"
                    f"{mentions}\n\n"
                    f"Please enable DMs so I can send you job matches:\n"
                    f"**Server Settings > Privacy Settings > Allow direct messages from server members**"
                )
        except Exception as exc:
            log.error("Failed to send DM-failure notice: %s", exc)

    return sent


async def _record_dm_statuses(
    statuses: dict[int, str], sent_at: dict[int, datetime],
) -> None:
    if not statuses:
        return
    try:
        async with get_session() as session:
            for uj_id, status in statuses.items():
                await session.execute(
                    update(UserJob)
                    .where(UserJob.id == uj_id)
                    .values(dm_status=status, dm_sent_at=sent_at.get(uj_id))
                )
            await session.commit()
    except Exception as exc:
        log.error("Recording DM status for %d deliveries failed: %s", len(statuses), exc)


# ═══════════════════════════════════════════════════════════════════════
# Orchestrator — runs all three stages in sequence
# ═══════════════════════════════════════════════════════════════════════


class ScrapeCycleBusy(RuntimeError):
    """A scrape cycle is already running in this process."""


# One cycle at a time, shared by the scheduler and /scrape_now. APScheduler
# only serialises its own runs; a /scrape_now during a scheduled cycle ran
# both, and each delivered the other's new matches — two DMs per job.
_cycle_lock = asyncio.Lock()


def cycle_running() -> bool:
    return _cycle_lock.locked()


async def run_scrape_cycle(
    bot: JobBot,
    tunnel_manager: SSHTunnelManager,
    alert_channel_id: int,
    use_semantic: bool = False,
    semantic_model: str = "all-mpnet-base-v2",
    semantic_threshold: float = 0.42,
    semantic_threshold_cv: float | None = None,
) -> int:
    """Execute one full scrape -> match -> deliver cycle.

    Returns the number of DMs sent. Raises ScrapeCycleBusy when another
    cycle is already running.
    """
    if _cycle_lock.locked():
        raise ScrapeCycleBusy("a scrape cycle is already running")
    async with _cycle_lock:
        return await _run_scrape_cycle(
            bot, tunnel_manager, alert_channel_id, use_semantic,
            semantic_model, semantic_threshold, semantic_threshold_cv,
        )


async def _run_scrape_cycle(
    bot: JobBot,
    tunnel_manager: SSHTunnelManager,
    alert_channel_id: int,
    use_semantic: bool,
    semantic_model: str,
    semantic_threshold: float,
    semantic_threshold_cv: float | None,
) -> int:
    from gosha.scrape_health import get_scrape_health

    log.info("=== Scrape cycle started ===")
    health = get_scrape_health()

    # Stage 1: Scrape
    jobs = await run_scrape_stage(tunnel_manager, health)

    # Health alerting happens BEFORE anything can return early: a cycle
    # that found nothing is exactly the cycle worth shouting about.
    await _post_health_alert(bot, alert_channel_id, health.last_report)

    if jobs:
        # Embed freshly scraped jobs so the feed and the matcher can rank
        # them (best-effort)
        try:
            from gosha.embeddings import embed_new_jobs
            await embed_new_jobs()
        except Exception as exc:
            log.warning("Job embedding step failed: %s", exc)

        # Group cross-board duplicates before matching (best-effort)
        try:
            from gosha.dedup import assign_dedup_groups
            await assign_dedup_groups()
        except Exception as exc:
            log.warning("Dedup step failed: %s", exc)

        # The Job objects from the scrape stage predate both steps: without
        # a reload the matcher saw no stored embedding (and re-encoded every
        # job once per search) and no duplicate groups.
        jobs = await _reload_jobs([job.id for job in jobs])

        # Stage 2: Match (with optional semantic scoring)
        matcher = None
        if use_semantic:
            matcher = SemanticMatcher(
                model_name=semantic_model, threshold=semantic_threshold,
                cv_threshold=semantic_threshold_cv,
            )
            # The first `available` loads the model; do that off the loop.
            if await asyncio.to_thread(lambda: matcher.available):
                log.info(
                    "Using semantic matching (model=%s, threshold=%.2f, with CV %.2f)",
                    semantic_model, matcher.threshold, matcher.cv_threshold,
                )
            else:
                log.warning("Semantic matching requested but not available — falling back to regex")
                matcher = None

        await run_match_stage(jobs, matcher)
    else:
        log.warning("No jobs found this cycle.")

    # Stage 3: Deliver — this cycle's matches and any earlier ones whose
    # DM failed for a transient reason.
    total_sent = await deliver_pending(bot)

    log.info("=== Scrape cycle finished — %d DMs sent ===", total_sent)
    return total_sent


async def _reload_jobs(job_ids: list[int]) -> list[Job]:
    if not job_ids:
        return []
    async with get_session() as session:
        result = await session.execute(
            select(Job).where(Job.id.in_(job_ids), Job.is_active.is_(True))
        )
        return list(result.scalars().all())


async def _post_health_alert(
    bot: JobBot, alert_channel_id: int, report: CycleReport | None,
) -> None:
    """Push scraper-health alerts to the ops channel.

    Deliberately silent on success. The previous behaviour — a message only
    when deliveries went out — meant a dead scraper looked exactly like a
    quiet week.
    """
    if not alert_channel_id or report is None or not report.should_alert:
        return
    try:
        channel = bot.get_channel(alert_channel_id)
        if channel is None:
            return
        breakdown = report.breakdown()
        await channel.send(  # type: ignore[union-attr]
            "**Scraper health**\n"
            + report.summary()
            + (f"\nscraped→kept: `{breakdown}`" if breakdown else "")
        )
    except Exception as exc:
        log.error("Failed to post scraper-health alert: %s", exc)


# ═══════════════════════════════════════════════════════════════════════
# Delivery helpers (kept for backward compat with existing tests)
# ═══════════════════════════════════════════════════════════════════════


async def get_undelivered_jobs(user_id: int, job_ids: list[int]) -> set[int]:
    """Return the subset of job_ids not yet delivered to this user."""
    if not job_ids:
        return set()
    async with get_session() as session:
        result = await session.execute(
            select(UserJob.job_id).where(
                UserJob.user_id == user_id,
                UserJob.job_id.in_(job_ids),
            )
        )
        already = {row[0] for row in result.all()}
    return set(job_ids) - already


async def record_delivery(
    user_id: int,
    job_id: int,
    subscription_id: int | None = None,
    relevance_score: float | None = None,
) -> UserJob:
    """Record that a job was delivered to a user."""
    async with get_session() as session:
        uj = UserJob(
            user_id=user_id,
            job_id=job_id,
            subscription_id=subscription_id,
            relevance_score=relevance_score,
        )
        session.add(uj)
        await session.commit()
        return uj


DM_SENT = "sent"
DM_CLOSED = "closed"  # Forbidden: DMs closed or the bot is blocked
DM_RETRY = "retry"    # anything else: try again next cycle


async def _dm_user(
    bot: JobBot, discord_id: int, embed: object, view: object | None = None,
) -> str:
    """Send an embed to a user via DM; return DM_SENT, DM_CLOSED or DM_RETRY.

    Never raises: one user's failure (a 503, a dropped connection) must not
    end the loop for everyone after them.
    """
    import discord as _discord

    try:
        user = bot.get_user(discord_id)
        if user is None:
            user = await bot.fetch_user(discord_id)
        dm = await user.create_dm()
        kwargs: dict = {"embed": embed}
        if view is not None:
            kwargs["view"] = view
        await dm.send(**kwargs)
        if view is not None and hasattr(view, "stop"):
            # Buttons are handled by the global on_interaction listener
            # (views.setup_interaction_handler); a live view would handle
            # every click a second time and stay in memory until restart.
            view.stop()
        return DM_SENT
    except _discord.Forbidden:
        log.warning("Cannot DM user %d — DMs are closed", discord_id)
        return DM_CLOSED
    except _discord.NotFound:
        log.warning("Discord user %d no longer exists", discord_id)
        return DM_CLOSED
    except Exception as exc:
        log.error("Failed to DM user %d (will retry): %s", discord_id, exc)
        return DM_RETRY


async def _send_text(bot: JobBot, discord_id: int, text: str) -> None:
    try:
        user = bot.get_user(discord_id) or await bot.fetch_user(discord_id)
        await (await user.create_dm()).send(text)
    except Exception as exc:
        log.warning("Follow-up DM to %d failed: %s", discord_id, exc)


# ═══════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════


_URL_MAX = 1024  # Job.url


def _fit(text: str, limit: int) -> str:
    """`text` cut to a column width, on a separator when there is one."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = max(cut.rfind(", "), cut.rfind(" "))
    return (cut[:boundary] if boundary > limit // 2 else cut).rstrip(" ,;-")


def _clean(value: object, default: str) -> str:
    """Clean a DataFrame cell value into a string."""
    if value is None:
        return default
    s = str(value)
    if s in ("nan", "None", ""):
        return default
    return s


def _parse_float(row: pd.Series, col: str) -> float | None:
    """Safely parse a float from a DataFrame row."""
    if col not in row or not pd.notna(row.get(col)):
        return None
    try:
        return float(row[col])
    except (ValueError, TypeError):
        return None


def _parse_datetime(row: pd.Series, col: str) -> datetime | None:
    """Safely parse a datetime (or date) from a DataFrame row."""
    if col not in row:
        return None
    value = row.get(col)
    try:
        if value is None or pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, datetime):
        return value
    if isinstance(value, pd.Timestamp):
        return value.to_pydatetime()
    try:
        parsed = pd.to_datetime(str(value), utc=True)
        return parsed.to_pydatetime()
    except (ValueError, TypeError):
        return None
