"""Cross-source salary normalisation.

Boards disagree about both currency and period: eJobs quotes monthly RON,
BestJobs monthly EUR, RemoteOK ANNUAL USD. Comparing the raw numbers — as
the salary filter used to — is meaningless, and it failed in the direction
users notice: a USD 3,000/year listing passed a "3000 minimum" filter while
a EUR 2,500/month role was excluded.
"""

from __future__ import annotations

import pytest

from gosha.salary import (
    meets_minimum,
    monthly_ron,
    normalise_range,
    rates_to_ron,
    to_ron,
)


class TestCurrencyConversion:
    def test_ron_is_the_base(self):
        assert to_ron(5000, "RON") == 5000
        assert to_ron(5000, "lei") == 5000

    def test_missing_currency_assumed_ron(self):
        assert to_ron(5000, None) == 5000

    def test_eur_and_usd(self):
        assert to_ron(1000, "EUR") == pytest.approx(5000, rel=0.2)
        assert to_ron(1000, "USD") == pytest.approx(4600, rel=0.2)

    def test_unknown_currency_is_not_guessed(self):
        assert to_ron(1000, "XYZ") is None

    def test_rates_can_be_overridden(self, monkeypatch):
        monkeypatch.setenv("GOSHA_FX_RATES", "EUR:6.0,XYZ:2.5")
        rates = rates_to_ron()
        assert rates["EUR"] == 6.0
        assert rates["XYZ"] == 2.5
        assert rates["RON"] == 1.0

    def test_garbage_override_is_ignored(self, monkeypatch):
        monkeypatch.setenv("GOSHA_FX_RATES", "EUR:notanumber,nonsense")
        assert rates_to_ron()["EUR"] == 5.0


class TestPeriodNormalisation:
    def test_monthly_passes_through(self):
        assert monthly_ron(6000, "RON", "monthly") == 6000

    def test_annual_usd_becomes_monthly_ron(self):
        """The RemoteOK case: 120,000 USD/year is not 120,000 anything/month."""
        result = monthly_ron(120_000, "USD", "yearly")
        assert result == pytest.approx(46_000, rel=0.05)

    def test_hourly(self):
        # 30 EUR/hour ~ 150 RON/hour * 168 h ~ 25,200 RON/month
        assert monthly_ron(30, "EUR", "hourly") == pytest.approx(25_200, rel=0.05)

    def test_jobspy_style_period_labels(self):
        assert monthly_ron(1200, "EUR", "year") == pytest.approx(500, rel=0.05)
        assert monthly_ron(1200, "EUR", "month") == pytest.approx(6000, rel=0.05)

    def test_unlabelled_large_amount_inferred_annual(self):
        """A source that omits the period still must not be read as monthly."""
        # 150,000 RON is not a monthly Romanian salary.
        assert monthly_ron(150_000, "RON", None) == pytest.approx(12_500, rel=0.05)

    def test_unlabelled_plausible_monthly_left_alone(self):
        assert monthly_ron(8000, "RON", None) == 8000

    def test_unlabelled_tiny_amount_inferred_hourly(self):
        assert monthly_ron(100, "RON", None) == pytest.approx(16_800, rel=0.05)

    def test_missing_and_nonsense_values(self):
        assert monthly_ron(None, "RON", "monthly") is None
        assert monthly_ron(0, "RON", "monthly") is None
        assert monthly_ron(-5, "RON", "monthly") is None
        assert monthly_ron("abc", "RON", "monthly") is None


class TestNormaliseRange:
    def test_range_is_ordered(self):
        low, high = normalise_range(9000, 5000, "RON", "monthly")
        assert (low, high) == (5000, 9000)

    def test_single_sided_range_stays_single_sided(self):
        assert normalise_range(5000, None, "RON", "monthly") == (5000, None)
        assert normalise_range(None, 9000, "RON", "monthly") == (None, 9000)

    def test_unknown_currency_yields_nothing(self):
        assert normalise_range(5000, 9000, "XYZ", "monthly") == (None, None)

    def test_sources_become_comparable(self):
        """The point of the whole module."""
        ejobs = normalise_range(8000, 12000, "RON", "monthly")
        bestjobs = normalise_range(1600, 2400, "EUR", "monthly")
        remoteok = normalise_range(90_000, 140_000, "USD", "yearly")

        # eJobs 8-12k RON and BestJobs 1.6-2.4k EUR are the same job.
        assert ejobs[0] == pytest.approx(bestjobs[0], rel=0.05)
        # RemoteOK's much larger raw numbers really are the better-paid role,
        # but only by ~4x, not the ~10x the raw figures implied.
        assert remoteok[0] > ejobs[1]
        assert remoteok[0] < ejobs[1] * 5


class TestMeetsMinimum:
    def test_no_filter_passes_everything(self):
        assert meets_minimum(None, 1000, 2000) is True

    def test_uses_the_upper_bound_when_present(self):
        assert meets_minimum(9000, 5000, 12000) is True
        assert meets_minimum(15000, 5000, 12000) is False

    def test_falls_back_to_the_lower_bound(self):
        assert meets_minimum(5000, 6000, None) is True
        assert meets_minimum(8000, 6000, None) is False

    def test_missing_salary_data_is_included(self):
        # Most Romanian postings omit salary; excluding them would empty
        # the feed for anyone who sets this filter.
        assert meets_minimum(9000, None, None) is True

    def test_the_bug_this_replaces(self):
        """Raw comparison let annual USD through and excluded monthly EUR."""
        annual_usd_min, annual_usd_max = normalise_range(3000, 3000, "USD", "yearly")
        monthly_eur_min, monthly_eur_max = normalise_range(2500, 2500, "EUR", "monthly")

        # A 3,000 USD/YEAR job does not clear a 3,000 RON/month floor...
        assert meets_minimum(3000, annual_usd_min, annual_usd_max) is False
        # ...and a 2,500 EUR/month job comfortably does.
        assert meets_minimum(3000, monthly_eur_min, monthly_eur_max) is True
