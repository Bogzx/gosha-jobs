"""Entry point: boots SSH tunnels, the scheduler, and the Discord bot."""

from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from gosha.bot import JobBot
from gosha.config import load_settings
from gosha.database import init_db
from gosha.pipeline import ScrapeCycleBusy, run_scrape_cycle
from gosha.ssh_tunnels import SSHTunnelManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


async def main() -> None:
    settings = load_settings()

    # ── Database ────────────────────────────────────────────────
    await init_db(settings.database_url)
    log.info("Database initialised")

    # ── CV storage: refuse plaintext outside dev, encrypt leftovers ──
    from gosha import cv_crypto
    from gosha.cover_letter import prepare_cv_storage

    cv_crypto.require_key_configured()
    prepare_cv_storage()

    # ── SSH Tunnels ─────────────────────────────────────────────
    tunnel_mgr = SSHTunnelManager(settings.vps_list)
    if settings.vps_list:
        await tunnel_mgr.start_all()
    else:
        log.warning("No VPS proxies configured — scraper will run without proxies")

    # ── Discord Bot ─────────────────────────────────────────────
    bot = JobBot()
    bot.tunnel_manager = tunnel_mgr
    bot.alert_channel_id = settings.alert_channel_id
    bot.settings = settings  # Make settings accessible to commands

    # ── Ops alerts (gosha/alerts.py) ────────────────────────────
    # DM to the owner (ALERT_DM_USER_IDS) only, deduplicated. State
    # files sit next to the heartbeat in data/ (a volume), so a deploy in
    # the middle of an outage neither re-pages the owner nor forgets the
    # yield baselines.
    from gosha.alerts import (
        alert_recipients,
        get_alerter,
        scheduler_event_conditions,
        sink_for_bot,
    )
    from gosha.heartbeat import DEFAULT_HEARTBEAT_PATH
    from gosha.scrape_health import check_data_quality, get_scrape_health

    state_dir = DEFAULT_HEARTBEAT_PATH.parent
    alerter = get_alerter()
    alerter.cooldown_seconds = settings.alert_cooldown_minutes * 60
    alerter.attach_state_file(state_dir / "alerts_state.json")
    get_scrape_health().attach_state_file(state_dir / "scrape_health.json")
    if not settings.alerts_enabled:
        log.warning("Ops alerts disabled (ALERTS_ENABLED=false): failures only reach the log.")
    elif not alert_recipients(settings):
        log.warning(
            "Ops alerts off: ALERT_DM_USER_IDS is empty, so scraper failures "
            "only reach the log. Set it to the owner's numeric Discord user ID."
        )
    else:
        log.info("Ops alerts will be DMed to %s", alert_recipients(settings))

    # ── Scheduler ───────────────────────────────────────────────
    scheduler = AsyncIOScheduler()

    async def _scrape_and_track(**kwargs: object) -> None:
        """Wrapper that tracks last scrape time on the bot."""
        import time as _time
        try:
            await run_scrape_cycle(**kwargs)
        except ScrapeCycleBusy:
            log.warning("Skipping the scheduled scrape: /scrape_now's cycle is still running.")
            return
        bot._last_scrape_at = _time.monotonic()

    # Interval triggers count from process start, so without an explicit
    # first run a 4-hour interval + frequent deploys would starve scraping
    # (every restart resets the timer). Kick the first cycle shortly after
    # boot; the interval continues from there.
    scheduler.add_job(
        _scrape_and_track,
        trigger=IntervalTrigger(minutes=settings.scrape_interval_minutes),
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=3),
        kwargs={
            "bot": bot,
            "tunnel_manager": tunnel_mgr,
            "alert_channel_id": settings.alert_channel_id,
            "use_semantic": settings.use_semantic_matching,
            "semantic_model": settings.semantic_model,
            "semantic_threshold": settings.semantic_threshold,
            "semantic_threshold_cv": settings.semantic_threshold_cv,
        },
        id="scrape_cycle",
        name="Periodic job scrape",
        replace_existing=True,
        # Scraping is serial across up to 19 expanded keyword terms and can
        # overrun the interval. APScheduler's default grace period is one
        # second, so an overrunning cycle meant the next one was dropped in
        # silence. Coalesce backlog into a single run and give it half an
        # interval of slack; anything still missed is logged loudly below.
        coalesce=True,
        misfire_grace_time=max(60, settings.scrape_interval_minutes * 30),
    )

    from apscheduler.events import (
        EVENT_JOB_ERROR,
        EVENT_JOB_EXECUTED,
        EVENT_JOB_MAX_INSTANCES,
        EVENT_JOB_MISSED,
    )

    # Jobs whose failure someone should hear about, not just the log. The
    # rest (outbox poll, heartbeat, backfills) log and retry on their own.
    alert_jobs = {"scrape_cycle", "deadlink_check", "cv_retention", "data_quality_check"}
    main_loop = asyncio.get_running_loop()

    async def _alert(failing: dict[str, str], cleared: dict[str, str]) -> None:
        try:
            await alerter.update(sink_for_bot(bot), failing, cleared, title="Scheduler")
        except Exception as exc:
            log.error("Failed to post scheduler alert: %s", exc)

    def _on_job_event(event: object) -> None:
        job_id = getattr(event, "job_id", "?")
        code = getattr(event, "code", None)
        if code == EVENT_JOB_ERROR:
            log.error("Scheduled job %s raised: %r", job_id, getattr(event, "exception", None))
        elif code == EVENT_JOB_MAX_INSTANCES:
            # An overrunning cycle fires this, not EVENT_JOB_MISSED.
            log.error("Scheduled job %s skipped: its previous run is still going.", job_id)
        elif code == EVENT_JOB_MISSED:
            log.error(
                "Scheduled job %s missed its run time — the previous cycle is "
                "still running or the loop is blocked.", job_id,
            )
        failing, cleared = scheduler_event_conditions(event, alert_jobs)
        if failing or cleared:
            # Listeners may run off the loop thread (executor-run jobs).
            main_loop.call_soon_threadsafe(
                lambda: main_loop.create_task(_alert(failing, cleared))
            )

    scheduler.add_listener(
        _on_job_event,
        EVENT_JOB_MISSED | EVENT_JOB_MAX_INSTANCES | EVENT_JOB_ERROR | EVENT_JOB_EXECUTED,
    )

    # What the cycles stored, judged from the database: descriptions
    # suddenly missing, an embedding backlog (the feed and semantic
    # matching are blind to unembedded jobs), no new job in a day.
    async def _data_quality_tick() -> None:
        report = await check_data_quality()
        await alerter.update(
            sink_for_bot(bot), report.conditions, report.cleared, title="Data quality",
        )

    scheduler.add_job(
        _data_quality_tick,
        trigger=IntervalTrigger(hours=1),
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=30),
        id="data_quality_check",
        name="Scrape data-quality check",
        replace_existing=True,
    )

    # Deliver web-enqueued Discord messages (outbox) every 30 seconds
    async def _outbox_tick() -> None:
        from gosha.outbox import process_outbox
        try:
            await process_outbox(bot)
        except Exception as exc:
            log.warning("Outbox processing failed: %s", exc)

    scheduler.add_job(
        _outbox_tick,
        trigger=IntervalTrigger(seconds=30),
        id="outbox_poller",
        name="Web outbox DM delivery",
        replace_existing=True,
    )

    # Backfill embeddings for jobs that predate the web platform (hourly),
    # plus salary normalisation for rows scraped before gosha/salary.py.
    async def _embed_backfill() -> None:
        from gosha.embeddings import embed_new_jobs
        try:
            await embed_new_jobs()
        except Exception as exc:
            log.warning("Embedding backfill failed: %s", exc)

        from gosha.services.jobs import backfill_salary_normalisation
        try:
            await backfill_salary_normalisation()
        except Exception as exc:
            log.warning("Salary normalisation backfill failed: %s", exc)

    scheduler.add_job(
        _embed_backfill,
        trigger=IntervalTrigger(hours=1),
        id="embed_backfill",
        name="Job embedding backfill",
        replace_existing=True,
    )

    # Expire postings whose URLs 404 (daily)
    async def _deadlink_tick() -> None:
        from gosha.deadlinks import check_dead_links
        try:
            await check_dead_links()
        except Exception as exc:
            log.warning("Dead-link check failed: %s", exc)

    # Same starvation concern as the scrape job: a 24h interval would
    # never fire if deploys restart the bot more often than daily.
    scheduler.add_job(
        _deadlink_tick,
        trigger=IntervalTrigger(hours=24),
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=45),
        id="deadlink_check",
        name="Dead job link detection",
        replace_existing=True,
    )

    # CV retention: delete CV data of users inactive > CV_RETENTION_MONTHS;
    # event retention (opt-in): events older than EVENT_RETENTION_MONTHS
    # (gosha/services/retention.py). Daily, first run an hour after boot.
    async def _cv_retention_tick() -> None:
        from gosha.services.retention import purge_old_events, purge_stale_cvs
        try:
            await purge_stale_cvs()
        except Exception as exc:
            log.warning("CV retention run failed: %s", exc)
        try:
            await purge_old_events()
        except Exception as exc:
            log.warning("Event retention run failed: %s", exc)

    scheduler.add_job(
        _cv_retention_tick,
        trigger=IntervalTrigger(hours=24),
        next_run_time=datetime.now(timezone.utc) + timedelta(hours=1),
        id="cv_retention",
        name="CV retention",
        replace_existing=True,
    )

    # Liveness stamp for the container healthcheck. If the event loop
    # wedges — the classic failure here — the scheduler stops firing, the
    # stamp goes stale, and the healthcheck turns the container unhealthy
    # instead of leaving it "up" and silently scraping nothing.
    from gosha.heartbeat import (
        HEARTBEAT_INTERVAL_SECONDS,
        start_watchdog,
        touch as _touch_heartbeat,
    )

    _touch_heartbeat()
    # Docker does not restart an "unhealthy" container; the watchdog exits
    # the process when the heartbeat stops, and the restart policy does.
    start_watchdog()
    scheduler.add_job(
        _touch_heartbeat,
        trigger=IntervalTrigger(seconds=HEARTBEAT_INTERVAL_SECONDS),
        id="liveness_heartbeat",
        name="Liveness heartbeat",
        replace_existing=True,
    )

    # Health-check tunnels every 5 minutes and restart dead ones
    if settings.vps_list:
        scheduler.add_job(
            tunnel_mgr.check_and_restart,
            trigger=IntervalTrigger(minutes=5),
            id="tunnel_health_check",
            name="SSH tunnel health check",
            replace_existing=True,
        )

    @bot.event
    async def on_ready() -> None:
        log.info(
            "Bot logged in as %s (id=%s)",
            bot.user,
            bot.user.id if bot.user else "?",
        )
        if not scheduler.running:
            scheduler.start()
            log.info(
                "Scheduler started — scraping every %d min",
                settings.scrape_interval_minutes,
            )

    # ── Graceful shutdown ───────────────────────────────────────
    loop = asyncio.get_running_loop()
    _shutting_down = False

    async def shutdown() -> None:
        nonlocal _shutting_down
        if _shutting_down:
            return
        _shutting_down = True
        log.info("Shutting down ...")
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await tunnel_mgr.stop_all()
        if not bot.is_closed():
            await bot.close()

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(shutdown()))
    except NotImplementedError:
        pass  # signal handlers not supported on Windows

    try:
        await bot.start(settings.discord_token)
    finally:
        await shutdown()


if __name__ == "__main__":
    asyncio.run(main())
