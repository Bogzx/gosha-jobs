"""Normalise salaries onto one comparable basis: gross RON per month.

Why this exists
---------------
Every source quotes salary in its own units:

* eJobs — monthly, RON
* BestJobs — monthly, EUR
* RemoteOK — **annual**, USD
* JobSpy (Indeed/LinkedIn/Glassdoor) — whatever the posting said

The stored `salary_min`/`salary_max` were raw numbers and the filter
compared them directly, so "minimum 3000" matched a 3,000 RON/month
internship (~EUR 600) and a USD 3,000/year listing equally, while
excluding an EUR 2,500/month role worth four times either. Sorting and
the salary chip in the UI had the same problem: 70k next to 8k, with no
indication that one was annual dollars and the other monthly lei.

Normalising once at ingest makes the filter mean something and costs
nothing at query time.

Rates
-----
Static, and deliberately so. A live FX feed would add a network
dependency and a failure mode to the ingest path for a filter whose job
is "roughly this much or better". They are approximate mid-market rates
and are used ONLY for comparison — the original amount and currency are
what get displayed. Override via env if they drift far enough to matter.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

# Units of RON per 1 unit of the given currency.
DEFAULT_RATES_TO_RON: dict[str, float] = {
    "RON": 1.0,
    "LEI": 1.0,
    "EUR": 5.0,
    "USD": 4.6,
    "GBP": 5.9,
    "CHF": 5.3,
    "PLN": 1.2,
    "HUF": 0.013,
    "BGN": 2.55,
    "SEK": 0.44,
    "DKK": 0.67,
    "NOK": 0.42,
    "CZK": 0.20,
}

MONTHS_PER_YEAR = 12

# Above these monthly-equivalent figures an amount is almost certainly an
# annual quote that arrived without a period label. A Romanian tech salary
# of 40,000 RON/month exists; 400,000 RON/month does not.
ANNUAL_GUESS_THRESHOLD_RON = 90_000

# Below these an amount is more likely hourly than monthly.
HOURLY_GUESS_CEILING_RON = 400
HOURS_PER_MONTH = 168

VALID_PERIODS = ("hourly", "daily", "weekly", "monthly", "yearly")


def rates_to_ron() -> dict[str, float]:
    """Conversion table, with an env override for operators who care.

    GOSHA_FX_RATES="EUR:5.07,USD:4.71" replaces those entries only.
    """
    rates = dict(DEFAULT_RATES_TO_RON)
    raw = os.getenv("GOSHA_FX_RATES", "")
    for pair in raw.split(","):
        if ":" not in pair:
            continue
        code, _, value = pair.partition(":")
        try:
            rates[code.strip().upper()] = float(value)
        except ValueError:
            log.warning("Ignoring unparseable GOSHA_FX_RATES entry %r", pair)
    return rates


def to_ron(amount: float, currency: str | None) -> float | None:
    """Convert an amount into RON. None when the currency is unknown."""
    code = (currency or "RON").strip().upper()
    rate = rates_to_ron().get(code)
    if rate is None:
        log.debug("Unknown currency %r — cannot normalise", currency)
        return None
    return amount * rate


def _period_factor(period: str | None, monthly_ron: float) -> float:
    """Multiplier that turns `period` amounts into monthly amounts."""
    normalised = (period or "").strip().lower()
    if normalised in ("year", "yearly", "annual", "annually", "yr", "per year"):
        return 1.0 / MONTHS_PER_YEAR
    if normalised in ("month", "monthly", "mo", "per month"):
        return 1.0
    if normalised in ("week", "weekly", "wk"):
        return 52.0 / MONTHS_PER_YEAR
    if normalised in ("day", "daily"):
        return 21.0
    if normalised in ("hour", "hourly", "hr"):
        return float(HOURS_PER_MONTH)

    # No usable label — infer from magnitude. Sources that do not state a
    # period are the ones that also mix conventions, so guessing beats
    # dropping the value entirely.
    if monthly_ron >= ANNUAL_GUESS_THRESHOLD_RON:
        return 1.0 / MONTHS_PER_YEAR
    if 0 < monthly_ron <= HOURLY_GUESS_CEILING_RON:
        return float(HOURS_PER_MONTH)
    return 1.0


def monthly_ron(
    amount: float | None, currency: str | None, period: str | None = None,
) -> float | None:
    """One amount as gross RON per month. None when it cannot be worked out."""
    if amount is None:
        return None
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None

    converted = to_ron(value, currency)
    if converted is None:
        return None

    return round(converted * _period_factor(period, converted), 2)


def normalise_range(
    salary_min: float | None,
    salary_max: float | None,
    currency: str | None,
    period: str | None = None,
) -> tuple[float | None, float | None]:
    """(min, max) as gross RON per month, ordered low-to-high."""
    low = monthly_ron(salary_min, currency, period)
    high = monthly_ron(salary_max, currency, period)

    # A single-sided range is common; keep it single-sided rather than
    # inventing the other bound.
    if low is not None and high is not None and low > high:
        low, high = high, low
    return low, high


def meets_minimum(
    minimum_ron_per_month: int | float | None,
    normalised_min: float | None,
    normalised_max: float | None,
) -> bool:
    """Does a job clear a monthly-RON floor?

    Jobs with no salary data pass — most Romanian postings omit it, and
    excluding them would empty the feed for anyone who sets the filter.
    """
    if minimum_ron_per_month is None:
        return True
    if normalised_max is not None:
        return normalised_max >= minimum_ron_per_month
    if normalised_min is not None:
        return normalised_min >= minimum_ron_per_month
    return True
