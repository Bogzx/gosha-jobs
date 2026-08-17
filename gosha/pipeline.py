"""Job-centric pipeline with three decoupled stages.

Stage 1 — SCRAPE:  Fetch from job boards, upsert into Job table.
Stage 2 — MATCH:   For each subscription, score jobs, enqueue deliveries.
Stage 3 — DELIVER: Pull from delivery queue, send via channels (Discord DM, etc.).

Each stage can run independently. The DB is the glue between them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import pandas as pd
from sqlalchemy import select

from gosha.database import get_session
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
        for _, row in df.iterrows():
            url = str(
                row.get("job_url")
                or row.get("job_url_direct")
                or row.get("link")
                or ""
            )
            if not url or url == "nan":
                continue

            title = _clean(row.get("title"), "Unknown Title")
            company = _clean(row.get("company"), "Unknown")
            location = _clean(row.get("location"), "")
            description = _clean(row.get("description"), "")
            source = _clean(row.get("site"), "unknown")

            salary_min = _parse_float(row, "min_amount")
            salary_max = _parse_float(row, "max_amount")
            salary_currency = (
                str(row["currency"])
                if "currency" in row and pd.notna(row.get("currency"))
                else None
            )
            # JobSpy calls it "interval"; our adapters emit the same key.
            salary_period = (
                str(row["interval"])
                if "interval" in row and pd.notna(row.get("interval"))
                else None
            )
            # Normalise once, here, so the filter and the sort can compare
            # eJobs' monthly RON with RemoteOK's annual USD.
            monthly_min, monthly_max = normalise_range(
                salary_min, salary_max, salary_currency, salary_period,
            )
            posted_at = _parse_datetime(row, "date_posted")

            result = await session.execute(select(Job).where(Job.url == url))
            existing = result.scalar_one_or_none()

            if existing:
                existing.last_seen_at = now
                existing.is_active = True
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
                )
                session.add(job)
                jobs.append(job)

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

    Returns all jobs found (new + updated). Per-source yields are folded
    into `health` (default: the process-wide tracker) so a board that has
    quietly stopped returning anything gets alerted on — see
    gosha/scrape_health.py.
    """
    from gosha.scrape_health import get_scrape_health
    from gosha.scraper import scrape_jobs_raw

    health = health or get_scrape_health()
    attempted: set[str] = set()
    per_source: dict[str, int] = {}

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
        df = await scrape_jobs_raw(
            tunnel_manager, keyword, location, max_age_days, boards=list(boards),
        )
        if not df.empty:
            df = filter_dataframe(df, keywords)
            jobs = await upsert_jobs(df)
            all_jobs.extend(jobs)

    # Extra sources (Romanian boards + RemoteOK): direct APIs, no proxies.
    # Additive for every subscription regardless of its boards list.
    extra_jobs = await _scrape_extra_sources(scrape_combos, attempted)
    all_jobs.extend(extra_jobs)

    # Deduplicate by ID
    seen_ids: set[int] = set()
    unique: list[Job] = []
    for j in all_jobs:
        if j.id not in seen_ids:
            seen_ids.add(j.id)
            unique.append(j)
            per_source[j.source] = per_source.get(j.source, 0) + 1

    health.record_cycle(attempted, per_source)

    log.info("Scrape stage complete: %d unique jobs", len(unique))
    return unique


# Keyword expansion can fan a broad term into ~19 searches; the extra
# sources are cheap JSON/HTML endpoints but we still cap politely.
MAX_EXTRA_TERMS = 6


async def _scrape_extra_sources(
    scrape_combos: dict[tuple, list[str]],
    attempted: set[str] | None = None,
) -> list[Job]:
    """Run the plugin scrapers for every (keyword, location) combo.

    Adapter names are added to `attempted` so a source that returns nothing
    is still counted as tried — otherwise a dead adapter just vanishes from
    the yield accounting instead of raising an alert.
    """
    from gosha.filters import expand_keyword
    from gosha.scrapers.base import SearchQuery

    try:
        from gosha.scrapers.registry import get_extra_scrapers
        scrapers = get_extra_scrapers()
    except Exception as exc:
        log.warning("Extra scrapers unavailable: %s", exc)
        return []

    if attempted is not None:
        attempted.update(s.name for s in scrapers)

    collected: list[Job] = []
    for (keyword, location, _boards, max_age_days), keywords in scrape_combos.items():
        terms = expand_keyword(keyword)[:MAX_EXTRA_TERMS]
        for scraper in scrapers:
            frames: list[pd.DataFrame] = []
            for term in terms:
                query = SearchQuery(
                    keyword=term, location=location, max_age_days=max_age_days,
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
            df = filter_dataframe(df, keywords)
            if not df.empty:
                jobs = await upsert_jobs(df)
                collected.extend(jobs)
                log.info(
                    "%s: %d jobs for %r in %r", scraper.name, len(jobs), keyword, location,
                )
    return collected


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
    if not matches_experience_level(job.title, sub.experience_levels):
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


def _title_relevant_for_any_keyword(title: str, keywords: list[str]) -> bool:
    """Check if title is relevant for at least one subscription keyword."""
    for kw in keywords:
        if title_is_relevant(title, kw):
            return True
    return False


async def match_jobs_for_subscription(
    sub: Subscription,
    jobs: list[Job],
    semantic_matcher: SemanticMatcher | None = None,
) -> list[tuple[Job, float]]:
    """Return (job, score) pairs for jobs matching a subscription.

    If a SemanticMatcher is provided and available, uses cosine similarity
    for scoring. Otherwise falls back to regex with score=1.0.
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
        query_emb = semantic_matcher.encode_subscription(
            keywords, sub.locations, sub.experience_levels,
        )
        if query_emb is not None:
            from gosha.embeddings import score_jobs_against_query

            scores = score_jobs_against_query(query_emb, candidates)
            matches = [
                (job, score)
                for job, score in zip(candidates, scores)
                if semantic_matcher.is_match(score)
            ]
            # Sort by score descending
            matches.sort(key=lambda x: x[1], reverse=True)
            return matches

    # Regex fallback path
    matches: list[tuple[Job, float]] = []
    for job in candidates:
        if keywords and not _title_relevant_for_any_keyword(job.title, keywords):
            continue
        matches.append((job, 1.0))
    return matches


async def run_match_stage(
    jobs: list[Job],
    semantic_matcher: SemanticMatcher | None = None,
) -> int:
    """Stage 2: match jobs against all active subscriptions and enqueue deliveries.

    Applies user feedback profiles to adjust scores when available.

    Returns total number of new deliveries enqueued.
    """
    async with get_session() as session:
        result = await session.execute(
            select(Subscription, User)
            .join(User)
            .where(Subscription.is_active.is_(True))
        )
        sub_user_pairs = result.all()

    if not sub_user_pairs:
        return 0

    # Build per-user feedback profiles for score adjustment
    from gosha.feedback import build_user_profile

    user_profiles: dict[int, object] = {}  # user_id -> UserPreferenceProfile

    # Build batch delivery items
    delivery_items: list[dict] = []

    for sub, user in sub_user_pairs:
        matches = await match_jobs_for_subscription(sub, jobs, semantic_matcher)

        # Build user profile lazily (once per user)
        if user.id not in user_profiles:
            try:
                user_profiles[user.id] = await build_user_profile(user.id)
            except Exception:
                user_profiles[user.id] = None

        profile = user_profiles.get(user.id)

        for job, score in matches:
            # Apply feedback-based score adjustment
            adjusted_score = score
            if profile and profile.has_data:
                adjusted_score = max(0.0, min(1.0, score + profile.score_adjustment(job)))

            delivery_items.append({
                "user_id": user.id,
                "job_id": job.id,
                "subscription_id": sub.id,
                "score": adjusted_score,
            })

    if not delivery_items:
        log.info("Match stage: no new matches found.")
        return 0

    created = await enqueue_deliveries_batch(delivery_items)
    log.info("Match stage complete: %d new deliveries enqueued", created)
    return created


# ═══════════════════════════════════════════════════════════════════════
# Stage 3 — DELIVER: send notifications to users
# ═══════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════
# Orchestrator — runs all three stages in sequence
# ═══════════════════════════════════════════════════════════════════════


async def run_scrape_cycle(
    bot: JobBot,
    tunnel_manager: SSHTunnelManager,
    alert_channel_id: int,
    use_semantic: bool = False,
    semantic_model: str = "all-mpnet-base-v2",
    semantic_threshold: float = 0.40,
) -> int:
    """Execute one full scrape -> match -> deliver cycle.

    Returns total number of new jobs delivered.
    """
    from gosha.scrape_health import get_scrape_health

    log.info("=== Scrape cycle started ===")
    cycle_start = datetime.now(timezone.utc)
    health = get_scrape_health()

    # Stage 1: Scrape
    jobs = await run_scrape_stage(tunnel_manager, health)

    # Health alerting happens BEFORE the early return: a cycle that found
    # nothing is exactly the cycle worth shouting about.
    await _post_health_alert(bot, alert_channel_id, health.last_report)

    if not jobs:
        log.warning("No jobs found — cycle complete.")
        return 0

    # Embed freshly scraped jobs so the web feed can rank them (best-effort)
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

    # Stage 2: Match (with optional semantic scoring)
    matcher = None
    if use_semantic:
        matcher = SemanticMatcher(
            model_name=semantic_model, threshold=semantic_threshold
        )
        if matcher.available:
            log.info("Using semantic matching (model=%s, threshold=%.2f)", semantic_model, semantic_threshold)
        else:
            log.warning("Semantic matching requested but not available — falling back to regex")
            matcher = None

    new_deliveries = await run_match_stage(jobs, matcher)
    if new_deliveries == 0:
        log.info("No new matches — cycle complete.")
        return 0

    # Stage 3: Deliver
    # Only deliver the newly enqueued items (from this cycle)
    total_sent = await _deliver_new(bot, cycle_start)

    log.info("=== Scrape cycle finished — %d new jobs sent ===", total_sent)
    return total_sent


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
        breakdown = ", ".join(
            f"{name}={count}" for name, count in sorted(report.per_source.items())
        )
        await channel.send(  # type: ignore[union-attr]
            "**Scraper health**\n"
            + report.summary()
            + (f"\n`{breakdown}`" if breakdown else "")
        )
    except Exception as exc:
        log.error("Failed to post scraper-health alert: %s", exc)


async def _deliver_new(bot: JobBot, since: datetime) -> int:
    """Deliver only UserJobs created since the given timestamp.

    Sends embeds with feedback buttons and emits delivery events.
    """
    from gosha.views import build_job_embed_with_buttons

    async with get_session() as session:
        result = await session.execute(
            select(UserJob, Job, User, Subscription)
            .join(Job, UserJob.job_id == Job.id)
            .join(User, UserJob.user_id == User.id)
            .outerjoin(Subscription, UserJob.subscription_id == Subscription.id)
            .where(UserJob.delivered_at >= since)
            .order_by(UserJob.delivered_at.asc())
        )
        items = result.all()

    # Deduplicate cross-board: same title+company from different sources
    # Group by (user_id, normalized_title, normalized_company), keep first
    seen_per_user: dict[tuple[int, str, str], bool] = {}
    deduped: list[tuple[UserJob, Job, User, Subscription | None]] = []
    skipped = 0

    for uj, job, user, sub in items:
        key = (
            user.id,
            (job.title or "").lower().strip(),
            (job.company or "").lower().strip(),
        )
        if key in seen_per_user:
            skipped += 1
            continue
        seen_per_user[key] = True
        deduped.append((uj, job, user, sub))

    if skipped:
        log.info("Cross-board dedup: skipped %d duplicate deliveries", skipped)

    sent = 0
    dm_failed_users: dict[int, str] = {}  # discord_id -> username hint

    for uj, job, user, sub in deduped:
        # Web-managed searches can opt out of Discord notifications;
        # matches still show up on the website.
        if sub is not None and not sub.notify_discord:
            continue

        # Skip users whose DMs already failed this cycle
        if user.discord_user_id in dm_failed_users:
            continue

        # Build match reason from subscription keywords
        match_info = None
        if sub:
            kw_str = ", ".join(sub.keywords[:3])[:80]
            loc_str = ", ".join(sub.locations[:2])[:60]
            match_info = f"{kw_str} in {loc_str}"
        embed, view = build_job_embed_with_buttons(job, uj.id, uj.relevance_score, match_info)
        success = await _dm_user(bot, user.discord_user_id, embed, view)
        if success:
            sent += 1
            # Emit delivery event (best-effort)
            try:
                from gosha.events import emit_job_delivered
                await emit_job_delivered(job.id, user.id, "discord_dm")
            except Exception:
                pass
        else:
            dm_failed_users[user.discord_user_id] = f"<@{user.discord_user_id}>"

    # Notify users with closed DMs in the alert channel
    if dm_failed_users and bot.alert_channel_id:
        try:
            channel = bot.get_channel(bot.alert_channel_id)
            if channel is not None:
                mentions = " ".join(dm_failed_users.values())
                await channel.send(
                    f"**I found jobs for you but can't deliver them!**\n"
                    f"{mentions}\n\n"
                    f"Please enable DMs so I can send you job matches:\n"
                    f"**Server Settings > Privacy Settings > Allow direct messages from server members**"
                )
        except Exception as exc:
            log.error("Failed to send DM-failure notice: %s", exc)

    return sent


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


async def _dm_user(
    bot: JobBot, discord_id: int, embed: object, view: object | None = None,
) -> bool:
    """Send an embed to a user via DM. Silently skip if DMs are closed.

    Returns True if the message was sent successfully.
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
        return True
    except _discord.Forbidden:
        log.warning("Cannot DM user %d — DMs are closed", discord_id)
        return False
    except _discord.HTTPException as exc:
        log.error("Failed to DM user %d: %s", discord_id, exc)
        return False


# ═══════════════════════════════════════════════════════════════════════
# Internal helpers
# ═══════════════════════════════════════════════════════════════════════


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
