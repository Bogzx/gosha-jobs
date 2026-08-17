"""Scraper yield monitoring — alerts on silence, not on success.

The original alerting fired only when a cycle *delivered* something
(`total_sent > 0`), so total scraper failure produced an info log and
nothing else: the owner would learn the product had died by noticing his
own DMs had stopped. This module inverts that. Nothing is said when jobs
flow; noise is made when they stop.

Two signals, because they fail differently:

* **Zero-yield cycle** — every source returned nothing. Usually infra:
  proxies down, DNS, the container wedged.
* **Per-source zero streak** — one board returns nothing for several
  cycles while the others are fine. Usually that board changed its
  markup/API and its adapter needs fixing. This is the failure mode that
  hides for weeks, because the feed keeps working on the other sources.

State is in-process and resets on restart. That is deliberate: after a
restart the first few cycles re-establish the baseline, and a scraper that
is genuinely dead trips the streak again within the hour.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Consecutive zero-yield cycles before a source is called dead. Three is
# deliberate: boards legitimately return nothing for a narrow keyword in a
# single cycle, but not three times running across every subscription.
ZERO_STREAK_ALERT = 3
# Re-alert cadence so a source that stays dead isn't silently forgotten
# after the first message scrolls away.
ZERO_STREAK_REPEAT = 12


@dataclass
class CycleReport:
    """What one scrape cycle yielded, and what deserves an alert."""

    total: int = 0
    per_source: dict[str, int] = field(default_factory=dict)
    attempted: list[str] = field(default_factory=list)
    zero_yield: bool = False
    zero_cycle_streak: int = 0
    dead_sources: list[str] = field(default_factory=list)
    recovered_sources: list[str] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)

    @property
    def should_alert(self) -> bool:
        return bool(self.alerts)

    def summary(self) -> str:
        """One-line human summary for logs and the Discord alert channel."""
        return "\n".join(self.alerts)


class ScrapeHealth:
    """Tracks per-source yield across cycles and decides when to shout."""

    def __init__(
        self,
        streak_threshold: int = ZERO_STREAK_ALERT,
        repeat_every: int = ZERO_STREAK_REPEAT,
    ) -> None:
        self.streak_threshold = streak_threshold
        self.repeat_every = repeat_every
        self.zero_cycle_streak = 0
        self.streaks: dict[str, int] = {}
        self.last_report: CycleReport | None = None

    def record_cycle(
        self,
        attempted: Iterable[str],
        per_source: Mapping[str, int],
    ) -> CycleReport:
        """Fold one cycle's yields into the running state.

        `attempted` is every source we asked for — a source that returned
        nothing has no key in `per_source`, so without it a dead source
        would simply disappear from the accounting instead of alerting.
        """
        attempted_list = sorted({s for s in attempted if s})
        counts = {s: int(per_source.get(s, 0)) for s in attempted_list}
        for source, count in per_source.items():
            counts.setdefault(source, int(count))

        total = sum(counts.values())
        report = CycleReport(
            total=total,
            per_source=counts,
            attempted=attempted_list,
            zero_yield=total == 0,
        )

        if total == 0:
            self.zero_cycle_streak += 1
        else:
            self.zero_cycle_streak = 0
        report.zero_cycle_streak = self.zero_cycle_streak

        for source, count in sorted(counts.items()):
            previous = self.streaks.get(source, 0)
            if count > 0:
                if previous >= self.streak_threshold:
                    report.recovered_sources.append(source)
                    report.alerts.append(
                        f"**{source}** recovered — {count} jobs after "
                        f"{previous} empty cycles."
                    )
                self.streaks[source] = 0
                continue

            streak = previous + 1
            self.streaks[source] = streak
            if streak == self.streak_threshold or (
                streak > self.streak_threshold
                and (streak - self.streak_threshold) % self.repeat_every == 0
            ):
                report.dead_sources.append(source)
                report.alerts.append(
                    f"**{source}** has returned 0 jobs for {streak} "
                    f"consecutive cycles — its adapter is probably broken."
                )

        if report.zero_yield:
            report.alerts.insert(
                0,
                f"**Scrape produced 0 jobs** across {len(attempted_list) or 'all'} "
                f"sources ({self.zero_cycle_streak} cycle(s) running). "
                f"Nothing is being delivered to anyone.",
            )

        if report.should_alert:
            log.error("Scrape health: %s", " | ".join(report.alerts))
        else:
            log.info(
                "Scrape health: %d jobs (%s)",
                total,
                ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no sources",
            )

        self.last_report = report
        return report


_health: ScrapeHealth | None = None


def get_scrape_health() -> ScrapeHealth:
    """Process-wide health tracker (the bot runs one scrape loop)."""
    global _health
    if _health is None:
        _health = ScrapeHealth()
    return _health


def reset_scrape_health() -> None:
    """Drop the tracker — tests and manual re-baselining."""
    global _health
    _health = None
