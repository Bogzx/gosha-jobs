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
from gosha.pipeline import run_scrape_cycle
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

    # ── Scheduler ───────────────────────────────────────────────
    scheduler = AsyncIOScheduler()

    async def _scrape_and_track(**kwargs: object) -> None:
        """Wrapper that tracks last scrape time on the bot."""
        import time as _time
        await run_scrape_cycle(**kwargs)
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

    def _on_job_missed(event: object) -> None:
        log.error(
            "Scheduled job %s missed its run time — the previous cycle is "
            "still running or the loop is blocked.",
            getattr(event, "job_id", "?"),
        )

    from apscheduler.events import EVENT_JOB_MISSED

    scheduler.add_listener(_on_job_missed, EVENT_JOB_MISSED)

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

    # Liveness stamp for the container healthcheck. If the event loop
    # wedges — the classic failure here — the scheduler stops firing, the
    # stamp goes stale, and the healthcheck turns the container unhealthy
    # instead of leaving it "up" and silently scraping nothing.
    from gosha.heartbeat import HEARTBEAT_INTERVAL_SECONDS, touch as _touch_heartbeat

    _touch_heartbeat()
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
