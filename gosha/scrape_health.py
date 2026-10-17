"""Scraper yield monitoring — alerts on silence, not on success.

The original alerting fired only when a cycle *delivered* something
(`total_sent > 0`), so total scraper failure produced an info log and
nothing else: the owner would learn the product had died by noticing his
own DMs had stopped. This module inverts that. Nothing is said when jobs
flow; noise is made when they stop.

In-process signals, judged on every scrape cycle (ScrapeHealth):

* **Zero-yield cycle** — every source returned nothing. Usually infra:
  proxies down, DNS, the container wedged.
* **Per-source zero streak** — one board returns nothing for several
  cycles while the others are fine. Usually that board changed its
  markup/API and its adapter needs fixing. This is the failure mode that
  hides for weeks, because the feed keeps working on the other sources.
* **Yield collapse** — a board that normally returns ~300 postings returns
  3 for two cycles running. Not zero, so the streak never trips, but
  pagination or a selector broke and the board is effectively gone.
  Judged against the median of the source's recent normal cycles.

Database signals, judged hourly (check_data_quality):

* **Empty descriptions** — a source whose recent postings mostly arrive
  without a description while they normally have one: the detail fetch or
  the description parser broke, and matching runs on titles alone.
* **Embedding backlog** — active postings hours old with no embedding: the
  embedding step is failing, so the feed and semantic DM matching cannot
  see new jobs at all.
* **No new jobs** — nothing first seen for a day while searches exist.

Every signal is a keyed *condition* (`conditions` failing, `cleared`
positively healthy, anything else unknown); gosha/alerts.py turns those
into owner DMs with cooldown and recovery messages.

Yields are counted on what each board RETURNED (unique postings), before
the pipeline's relevance filter. Counting after it made a working board
look dead whenever no subscriber's keyword matched its postings — the
alerts said "adapter probably broken" about boards that were fine. The
post-filter counts are still reported alongside, as `kept`.

A source is only judged when it was attempted, and the pipeline only
attempts a source whose applies_to() accepts some subscription's location:
RemoteOK used to "return 0 jobs for 57 consecutive cycles" simply because
nobody searched remote.

State is in-process unless a state file is attached (main.py attaches
one under data/), so streaks and yield baselines survive deploys.
"""

from __future__ import annotations

import hashlib
import json
import logging
import statistics
from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Consecutive zero-yield cycles before a source is called dead. Three is
# deliberate: boards legitimately return nothing for a narrow keyword in a
# single cycle, but not three times running across every subscription.
ZERO_STREAK_ALERT = 3
# Re-log cadence so a source that stays dead isn't silently forgotten in
# the logs. (The owner's reminders run on the alerter's cooldown instead.)
ZERO_STREAK_REPEAT = 12
# Consecutive all-zero cycles before the owner is paged. One empty cycle
# is often a blip (a proxy reconnecting, a DNS hiccup) and paging on it
# meant an alert and a "recovered" an hour apart; two in a row is not.
ZERO_CYCLES_ALERT = 2

# Yield collapse: below COLLAPSE_RATIO × the source's baseline (median of
# its last BASELINE_WINDOW non-collapsed, non-zero cycles) for
# COLLAPSE_CYCLES cycles running. Sources whose baseline is under
# COLLAPSE_MIN_BASELINE are too small to judge (5 → 1 is noise). After
# REBASELINE_AFTER collapsed cycles the new level is accepted as normal —
# e.g. the subscriptions that drove the old volume were deleted.
# Deleting the subscription that drove a board's volume looks exactly like
# a collapse. So when a search a board ran last cycle is gone (the pipeline
# passes the searches), that board gets SEARCH_CHANGE_GRACE cycles in
# which a drop below the threshold is taken as its new normal instead of
# an alert. Its baseline is kept otherwise: an edit that does not cut the
# yield costs nothing, and added searches never reset anything (they can
# only add postings). Collapse detection is never blind for more than
# those cycles, however often users edit.
COLLAPSE_RATIO = 0.25
COLLAPSE_MIN_BASELINE = 20
COLLAPSE_CYCLES = 2
BASELINE_WINDOW = 8
BASELINE_MIN_SAMPLES = 3
REBASELINE_AFTER = 24
SEARCH_CHANGE_GRACE = COLLAPSE_CYCLES
# A source missing from this many cycles in a row is forgotten (nobody
# searches where it applies any more). Not after one: a registry that
# failed to load for one cycle must not wipe every extra source's state.
RETIRE_AFTER_CYCLES = 24

KEY_ZERO_YIELD = "scrape:zero_yield"


def dead_key(source: str) -> str:
    return f"scrape:dead:{source}"


def collapse_key(source: str) -> str:
    return f"scrape:collapse:{source}"


@dataclass
class CycleReport:
    """What one scrape cycle yielded, and what deserves an alert."""

    total: int = 0
    # Unique postings each source returned (pre-filter).
    per_source: dict[str, int] = field(default_factory=dict)
    # How many of them passed the relevance filter and were stored.
    kept: dict[str, int] = field(default_factory=dict)
    attempted: list[str] = field(default_factory=list)
    zero_yield: bool = False
    zero_cycle_streak: int = 0
    dead_sources: list[str] = field(default_factory=list)
    recovered_sources: list[str] = field(default_factory=list)
    collapsed_sources: list[str] = field(default_factory=list)
    # Log lines for this cycle (state transitions only).
    alerts: list[str] = field(default_factory=list)
    # Keyed conditions for gosha/alerts.py: failing now / known healthy.
    conditions: dict[str, str] = field(default_factory=dict)
    cleared: dict[str, str] = field(default_factory=dict)
    # Alert keys of sources no longer attempted at all (nobody searches
    # where they apply): dropped from the alert state without a message.
    retired: list[str] = field(default_factory=list)
    # Set once gosha/alerts.py has judged it, so a cycle that records
    # nothing (no subscriptions) cannot re-send the previous cycle's alerts.
    published: bool = False

    @property
    def should_alert(self) -> bool:
        return bool(self.alerts)

    def summary(self) -> str:
        """This cycle's state transitions, one per line, for the log."""
        return "\n".join(self.alerts)

    def breakdown(self) -> str:
        """`source=scraped→kept` per source, e.g. `ejobs=212→15`."""
        parts = []
        for name, count in sorted(self.per_source.items()):
            if name in self.kept:
                parts.append(f"{name}={count}→{self.kept[name]}")
            else:
                parts.append(f"{name}={count}")
        return ", ".join(parts)


class ScrapeHealth:
    """Tracks per-source yield across cycles and decides when to shout."""

    def __init__(
        self,
        streak_threshold: int = ZERO_STREAK_ALERT,
        repeat_every: int = ZERO_STREAK_REPEAT,
        collapse_ratio: float = COLLAPSE_RATIO,
        collapse_min_baseline: int = COLLAPSE_MIN_BASELINE,
        collapse_cycles: int = COLLAPSE_CYCLES,
        baseline_window: int = BASELINE_WINDOW,
        baseline_min_samples: int = BASELINE_MIN_SAMPLES,
        rebaseline_after: int = REBASELINE_AFTER,
        zero_cycles_alert: int = ZERO_CYCLES_ALERT,
        state_path: Path | None = None,
    ) -> None:
        self.zero_cycles_alert = zero_cycles_alert
        self.streak_threshold = streak_threshold
        self.repeat_every = repeat_every
        self.collapse_ratio = collapse_ratio
        self.collapse_min_baseline = collapse_min_baseline
        self.collapse_cycles = collapse_cycles
        self.baseline_window = baseline_window
        self.baseline_min_samples = baseline_min_samples
        self.rebaseline_after = rebaseline_after
        self.zero_cycle_streak = 0
        self.streaks: dict[str, int] = {}
        # Recent normal yields per source, oldest first.
        self.history: dict[str, list[int]] = {}
        self.collapse_streaks: dict[str, int] = {}
        # Per source: fingerprints of the searches it ran last cycle, the
        # cycles of grace left after one was removed, and how many cycles
        # running it has not been attempted.
        self.searches: dict[str, list[str]] = {}
        self.grace: dict[str, int] = {}
        self.absent: dict[str, int] = {}
        self.last_report: CycleReport | None = None
        self.state_path = state_path
        if state_path is not None:
            self._load()

    # ── persistence ──────────────────────────────────────────────
    def attach_state_file(self, path: Path) -> None:
        self.state_path = path
        self._load()

    def _load(self) -> None:
        if self.state_path is None:
            return
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.zero_cycle_streak = int(raw.get("zero_cycle_streak", 0))
            self.streaks = {str(k): int(v) for k, v in raw.get("streaks", {}).items()}
            self.history = {
                str(k): [int(x) for x in v][-self.baseline_window:]
                for k, v in raw.get("history", {}).items()
            }
            self.collapse_streaks = {
                str(k): int(v) for k, v in raw.get("collapse_streaks", {}).items()
            }
            self.searches = {
                str(k): [str(x) for x in v] for k, v in raw.get("searches", {}).items()
            }
            self.grace = {str(k): int(v) for k, v in raw.get("grace", {}).items()}
            self.absent = {str(k): int(v) for k, v in raw.get("absent", {}).items()}
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            log.warning("Ignoring unreadable scrape-health state %s: %s", self.state_path, exc)

    def _save(self) -> None:
        if self.state_path is None:
            return
        data = {
            "zero_cycle_streak": self.zero_cycle_streak,
            "streaks": self.streaks,
            "history": self.history,
            "collapse_streaks": self.collapse_streaks,
            "searches": self.searches,
            "grace": self.grace,
            "absent": self.absent,
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError as exc:
            log.warning("Could not persist scrape-health state to %s: %s", self.state_path, exc)

    def baseline(self, source: str) -> float | None:
        """Median of the source's recent normal yields, once there are enough."""
        hist = self.history.get(source, [])
        if len(hist) < self.baseline_min_samples:
            return None
        return float(statistics.median(hist))

    # ── per-cycle judgement ──────────────────────────────────────
    def record_cycle(
        self,
        attempted: Iterable[str],
        per_source: Mapping[str, int],
        kept: Mapping[str, int] | None = None,
        searches: Mapping[str, Iterable[Hashable]] | Iterable[Hashable] | None = None,
    ) -> CycleReport:
        """Fold one cycle's yields into the running state.

        `attempted` is every source we asked for — a source that returned
        nothing has no key in `per_source`, so without it a dead source
        would simply disappear from the accounting instead of alerting.
        `per_source` is what each source returned; `kept` (optional) what
        survived the relevance filter, reported but not judged.
        `searches` (optional) identifies the searches this cycle ran, per
        source ({source: [search, …]}) or for all sources at once (e.g. the
        (keyword, location, boards, max_age) combos). A source whose
        searches lost one gets a grace period for a yield drop.
        """
        attempted_list = sorted({s for s in attempted if s})
        counts = {s: int(per_source.get(s, 0)) for s in attempted_list}
        for source, count in per_source.items():
            counts.setdefault(source, int(count))
        kept_counts: dict[str, int] = {}
        if kept is not None:
            kept_counts = {s: int(kept.get(s, 0)) for s in counts}

        total = sum(counts.values())
        report = CycleReport(
            total=total,
            per_source=counts,
            kept=kept_counts,
            attempted=attempted_list,
            zero_yield=total == 0,
        )

        if total == 0:
            self.zero_cycle_streak += 1
        else:
            self.zero_cycle_streak = 0
        report.zero_cycle_streak = self.zero_cycle_streak

        if searches is not None:
            self._track_searches(searches, set(counts))
        if counts:
            self._retire_unattempted(report, set(counts))

        for source, count in sorted(counts.items()):
            self._judge_dead(report, source, count)
            self._judge_collapse(report, source, count)
            if self.grace.get(source, 0) > 0:
                self.grace[source] -= 1
                if not self.grace[source]:
                    del self.grace[source]

        if report.zero_yield and counts:
            text = (
                f"**Scrape produced 0 jobs** across {len(attempted_list) or 'all'} "
                f"sources ({self.zero_cycle_streak} cycle(s) running). "
                f"Nothing is being delivered to anyone."
            )
            report.alerts.insert(0, text)
            if self.zero_cycle_streak >= self.zero_cycles_alert:
                report.conditions[KEY_ZERO_YIELD] = text
            # Every source is at zero; one line says it better than seven
            # "adapter probably broken" lines.
            for source in counts:
                report.conditions.pop(dead_key(source), None)
        elif total > 0:
            report.cleared[KEY_ZERO_YIELD] = (
                f"Scraping produces jobs again — {total} this cycle."
            )

        if report.should_alert:
            log.error("Scrape health: %s", " | ".join(report.alerts))
        else:
            log.info(
                "Scrape health: %d jobs scraped (%s)",
                total, report.breakdown() or "no sources",
            )

        self.last_report = report
        self._save()
        return report

    def _retire_unattempted(self, report: CycleReport, current: set[str]) -> None:
        """Forget sources not attempted for RETIRE_AFTER_CYCLES cycles.

        Their streaks and baselines would otherwise sit in the state file
        forever, and an alert raised while they were attempted would stay
        "active" with nothing left to clear it.
        """
        tracked = set(self.streaks) | set(self.history) | set(self.collapse_streaks)
        for source in current:
            self.absent.pop(source, None)
        for source in sorted(tracked - current):
            self.absent[source] = self.absent.get(source, 0) + 1
            if self.absent[source] < RETIRE_AFTER_CYCLES:
                continue
            for state in (self.streaks, self.history, self.collapse_streaks,
                          self.searches, self.grace, self.absent):
                state.pop(source, None)
            report.retired.extend([dead_key(source), collapse_key(source)])

    def _track_searches(
        self, searches: Mapping[str, Iterable[Hashable]] | Iterable[Hashable],
        current: set[str],
    ) -> None:
        def prints(items: Iterable[Hashable]) -> list[str]:
            return sorted({
                hashlib.sha256(repr(item).encode("utf-8")).hexdigest()[:12] for item in items
            })

        if isinstance(searches, Mapping):
            by_source = {str(k): prints(v) for k, v in searches.items()}
        else:
            shared = prints(searches)
            by_source = {source: shared for source in current}
        for source, now in by_source.items():
            before = self.searches.get(source)
            if before is not None and set(before) - set(now):
                log.info("Scrape health: %s lost a search; a yield drop in the next "
                         "%d cycles is taken as its new normal.", source, SEARCH_CHANGE_GRACE)
                self.grace[source] = SEARCH_CHANGE_GRACE
            self.searches[source] = now

    def _judge_dead(self, report: CycleReport, source: str, count: int) -> None:
        previous = self.streaks.get(source, 0)
        if count > 0:
            if previous >= self.streak_threshold:
                report.recovered_sources.append(source)
                report.alerts.append(
                    f"**{source}** recovered — {count} jobs after "
                    f"{previous} empty cycles."
                )
            report.cleared[dead_key(source)] = f"**{source}** recovered — {count} jobs this cycle."
            self.streaks[source] = 0
            return

        streak = previous + 1
        self.streaks[source] = streak
        if streak < self.streak_threshold:
            return
        text = (
            f"**{source}** has returned 0 jobs for {streak} "
            f"consecutive cycles — its adapter is probably broken."
        )
        report.conditions[dead_key(source)] = text
        if streak == self.streak_threshold or (
            (streak - self.streak_threshold) % self.repeat_every == 0
        ):
            report.dead_sources.append(source)
            report.alerts.append(text)

    def _judge_collapse(self, report: CycleReport, source: str, count: int) -> None:
        if count == 0:
            # Zero is the dead-source signal's job; it neither feeds nor
            # resets the baseline.
            return
        key = collapse_key(source)
        baseline = self.baseline(source)
        hist = self.history.setdefault(source, [])
        if (
            baseline is not None
            and baseline >= self.collapse_min_baseline
            and count < self.collapse_ratio * baseline
        ):
            if self.grace.get(source, 0) > 0:
                # A search this board ran is gone: that explains the drop.
                hist[:] = [count]
                self.collapse_streaks[source] = 0
                report.cleared[key] = (
                    f"**{source}**: one of its searches was removed, so ~{count} jobs "
                    f"per cycle (was ~{baseline:.0f}) is taken as its new normal."
                )
                return
            streak = self.collapse_streaks.get(source, 0) + 1
            self.collapse_streaks[source] = streak
            if streak >= self.rebaseline_after:
                hist[:] = [count]
                self.collapse_streaks[source] = 0
                report.cleared[key] = (
                    f"**{source}** has stayed at ~{count} jobs per cycle for {streak} "
                    f"cycles (was ~{baseline:.0f}); accepting that as its new normal."
                )
                return
            if streak >= self.collapse_cycles:
                text = (
                    f"**{source}** yield collapsed: {count} jobs this cycle vs a "
                    f"baseline of ~{baseline:.0f} ({streak} cycles running). "
                    f"Pagination or a selector probably broke."
                )
                report.conditions[key] = text
                if streak == self.collapse_cycles:
                    report.collapsed_sources.append(source)
                    report.alerts.append(text)
            return

        self.collapse_streaks[source] = 0
        hist.append(count)
        del hist[: -self.baseline_window]
        if baseline is not None:
            report.cleared[key] = (
                f"**{source}** yield is back to normal: {count} jobs (baseline ~{baseline:.0f})."
            )


_health: ScrapeHealth | None = None


def get_scrape_health() -> ScrapeHealth:
    """Process-wide health tracker (the bot runs one scrape loop)."""
    global _health
    if _health is None:
        _health = ScrapeHealth()
    return _health


def reset_scrape_health() -> None:
    """Drop the tracker — tests and manual re-baselining."""
    global _health, _previous_cohort
    _health = None
    _previous_cohort = None


# ═══════════════════════════════════════════════════════════════════════
# Database-side checks (hourly; survive restarts by construction)
# ═══════════════════════════════════════════════════════════════════════

# A description shorter than this is a stub ("See posting"), not content.
MIN_DESCRIPTION_CHARS = 40
EMPTY_DESC_WINDOW = timedelta(hours=24)
EMPTY_DESC_BASELINE = timedelta(days=8)
EMPTY_DESC_MIN_JOBS = 10
EMPTY_DESC_ALERT_RATE = 0.8
EMPTY_DESC_NORMAL_RATE = 0.5
# The scrape cycle and the hourly backfill both embed; anything older
# than this without a vector means both are failing.
EMBED_GRACE = timedelta(hours=3)
EMBED_BACKLOG_ALERT = 10
# Two days, not one: a quiet weekend on narrow searches is normal.
NO_NEW_JOBS_HOURS = 48

KEY_EMBEDDINGS = "data:embeddings"
KEY_NO_NEW_JOBS = "data:no_new_jobs"


# Progress of a backfill (typically re-embedding everything after a model
# change), measured on a fixed cohort: at each check, how many jobs first
# seen before the grace cutoff carry a current-model embedding, active or
# not. If that number grew since the previous check, embedding works and
# is catching up. (A shrinking backlog was the old measure; jobs going
# inactive shrink it too.) (cutoff, embedded count) of the last check.
_previous_cohort: tuple[datetime, int] | None = None


def descriptions_key(source: str) -> str:
    return f"data:descriptions:{source}"


@dataclass
class QualityReport:
    conditions: dict[str, str] = field(default_factory=dict)
    cleared: dict[str, str] = field(default_factory=dict)
    # source → (recent jobs, recent empty, baseline jobs, baseline empty)
    descriptions: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)
    embedding_backlog: int = 0
    last_new_job_at: datetime | None = None


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def judge_descriptions(
    report: QualityReport, source: str, recent: int, recent_empty: int,
    base: int, base_empty: int,
) -> None:
    report.descriptions[source] = (recent, recent_empty, base, base_empty)
    if recent < EMPTY_DESC_MIN_JOBS:
        return  # too few postings to tell
    rate = recent_empty / recent
    key = descriptions_key(source)
    if rate < EMPTY_DESC_NORMAL_RATE:
        report.cleared[key] = (
            f"**{source}** descriptions are back: {100 - round(rate * 100)}% of "
            f"{recent} recent postings have one."
        )
        return
    if rate < EMPTY_DESC_ALERT_RATE or base < EMPTY_DESC_MIN_JOBS:
        return
    base_rate = base_empty / base
    if base_rate > EMPTY_DESC_NORMAL_RATE:
        return  # this source never had descriptions; nothing broke
    report.conditions[key] = (
        f"**{source}**: {round(rate * 100)}% of the {recent} postings scraped in the "
        f"last 24h have no description (normally ~{round(base_rate * 100)}%). The detail "
        f"fetch or description parser is probably broken; matching sees titles only."
    )


async def check_data_quality(now: datetime | None = None) -> QualityReport:
    """Judge what the scrape cycles stored: descriptions, embeddings, freshness."""
    global _previous_cohort
    from sqlalchemy import case, func, or_, select

    from gosha.database import get_session
    from gosha.embeddings import current_model_clause, stale_model_clause
    from gosha.models import Job, Subscription

    now = now or datetime.now(timezone.utc)
    report = QualityReport()
    recent_since = now - EMPTY_DESC_WINDOW
    base_since = now - EMPTY_DESC_BASELINE
    empty = or_(
        Job.description.is_(None),
        func.length(func.trim(Job.description)) < MIN_DESCRIPTION_CHARS,
    )
    is_recent = Job.first_seen_at >= recent_since

    async with get_session() as session:
        rows = (await session.execute(
            select(
                Job.source,
                func.sum(case((is_recent, 1), else_=0)),
                func.sum(case((is_recent & empty, 1), else_=0)),
                func.sum(case((~is_recent, 1), else_=0)),
                func.sum(case((~is_recent & empty, 1), else_=0)),
            )
            # Deactivated postings (expired, dead links) are no longer
            # matched or shown, so their text no longer matters: 22 expired
            # BestJobs rows held the 2026-10-16 alert open after the rest
            # had their text.
            .where(Job.first_seen_at >= base_since, Job.is_active.is_(True))
            .group_by(Job.source)
        )).all()

        backlog, reembed = (await session.execute(
            select(
                func.count(Job.id),
                # Embedded, but by another model: a model change's backfill.
                func.sum(case((Job.embedding.isnot(None), 1), else_=0)),
            ).where(
                Job.is_active.is_(True),
                Job.first_seen_at < now - EMBED_GRACE,
                or_(Job.embedding.is_(None), stale_model_clause(Job.embedding_model)),
            )
        )).one()
        backlog, reembed = int(backlog or 0), int(reembed or 0)

        embedded = Job.embedding.isnot(None) & current_model_clause(Job.embedding_model)

        async def embedded_before(cutoff: datetime) -> int:
            return int((await session.execute(
                select(func.count(Job.id)).where(Job.first_seen_at < cutoff, embedded)
            )).scalar() or 0)

        cutoff = now - EMBED_GRACE
        embedded_now = await embedded_before(cutoff)
        progressing: bool | None = None
        if _previous_cohort is not None:
            then_cutoff, then_count = _previous_cohort
            progressing = await embedded_before(then_cutoff) > then_count

        last_new = (await session.execute(select(func.max(Job.first_seen_at)))).scalar()
        active_subs = (await session.execute(
            select(func.count(Subscription.id)).where(Subscription.is_active.is_(True))
        )).scalar() or 0

    for source, recent, recent_empty, base, base_empty in rows:
        judge_descriptions(
            report, str(source), int(recent or 0), int(recent_empty or 0),
            int(base or 0), int(base_empty or 0),
        )

    _previous_cohort = (cutoff, embedded_now)
    report.embedding_backlog = backlog
    if backlog >= EMBED_BACKLOG_ALERT and progressing:
        log.info("Embedding backlog %d, but older jobs are being embedded: a backfill "
                 "is progressing.", backlog)
    elif backlog >= EMBED_BACKLOG_ALERT and progressing is None and reembed:
        # Just started, mid model change: judge it at the next check, once
        # it is clear whether the backfill is moving.
        log.info("Embedding backlog %d (%d from a model change); judging next hour.",
                 backlog, reembed)
    elif backlog >= EMBED_BACKLOG_ALERT:
        report.conditions[KEY_EMBEDDINGS] = (
            f"**{backlog} active jobs** first seen over {int(EMBED_GRACE.total_seconds() // 3600)}h "
            f"ago still have no embedding. The embedding step is failing (model "
            f"missing, out of memory?), so the web feed and semantic DM matching "
            f"cannot see new postings."
        )
    else:
        report.cleared[KEY_EMBEDDINGS] = "Job embeddings are keeping up again."

    report.last_new_job_at = _aware(last_new)
    if report.last_new_job_at is not None and active_subs:
        age = now - report.last_new_job_at
        if age >= timedelta(hours=NO_NEW_JOBS_HOURS):
            report.conditions[KEY_NO_NEW_JOBS] = (
                f"**No new job** has been stored for {int(age.total_seconds() // 3600)}h "
                f"(last: {report.last_new_job_at:%Y-%m-%d %H:%M} UTC) although "
                f"{active_subs} searches are active."
            )
        else:
            report.cleared[KEY_NO_NEW_JOBS] = "New jobs are being stored again."

    if report.conditions:
        log.error("Data quality: %s", " | ".join(report.conditions.values()))
    return report
