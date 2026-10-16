"""What a match DM shows: each field from the right column, cleaned.

DM audit on production (2026-10-16, 130 sent DMs): salaries had no
period, so "17 - 98 USD" (hourly) and "4,500 - 6,500 EUR" (monthly) looked
alike; no posted date was shown although most postings have one; a
BestJobs location listing 25 cities was cut mid-word.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gosha.models import Job
from gosha.views import build_job_embed_with_buttons

NOW = datetime.now(timezone.utc)


def _job(**kwargs) -> Job:
    fields = dict(
        id=7, url="https://www.ejobs.ro/user/locuri-de-munca/qa-engineer/1",
        title="QA Engineer", company="Acme", location="Cluj-Napoca", source="ejobs",
        description="Manual testing.", first_seen_at=NOW,
    )
    fields.update(kwargs)
    return Job(**fields)


def _fields(job: Job, **kwargs) -> dict[str, str]:
    embed, _view = build_job_embed_with_buttons(job, 1, kwargs.pop("score", None), **kwargs)
    return {field.name: field.value for field in embed.fields}


@pytest.mark.parametrize("salary, shown", [
    (dict(salary_min=4500, salary_max=6500, salary_currency="EUR", salary_period="monthly"),
     "4,500 – 6,500 EUR / month"),
    (dict(salary_min=17, salary_max=98, salary_currency="USD", salary_period="hourly"),
     "17 – 98 USD / hour"),
    (dict(salary_min=60000, salary_max=None, salary_currency="USD", salary_period="yearly"),
     "from 60,000 USD / year"),
    (dict(salary_min=None, salary_max=7000, salary_currency="RON", salary_period="monthly"),
     "up to 7,000 RON / month"),
    (dict(salary_min=5000, salary_max=5000, salary_currency="RON", salary_period=None),
     "5,000 RON"),
])
def test_salary_shows_its_period(salary, shown):
    assert _fields(_job(**salary))["Salary"].startswith(shown)


def test_a_foreign_salary_also_shows_monthly_ron():
    job = _job(salary_min=4500, salary_max=6500, salary_currency="EUR", salary_period="monthly",
               salary_monthly_min_ron=22400, salary_monthly_max_ron=32300)
    assert _fields(job)["Salary"] == "4,500 – 6,500 EUR / month\n≈ 22,400 – 32,300 RON / month"
    ron = _job(salary_min=5000, salary_max=7000, salary_currency="RON", salary_period="monthly",
               salary_monthly_min_ron=5000, salary_monthly_max_ron=7000)
    assert "≈" not in _fields(ron)["Salary"]  # already RON per month


def test_no_salary_is_said_plainly():
    assert _fields(_job(salary_min=0, salary_max=None))["Salary"] == "Not listed"


@pytest.mark.parametrize("posted, first_seen, shown", [
    (NOW, NOW, "Today"),
    (NOW - timedelta(days=1), NOW, "Yesterday"),
    (NOW - timedelta(days=3), NOW, "3 days ago"),
    (None, NOW - timedelta(days=2), "First seen 2 days ago"),
    (None, NOW, "First seen today"),
    (NOW + timedelta(days=2), NOW, "Today"),  # a refresh date in the future
])
def test_posted_date(posted, first_seen, shown):
    assert _fields(_job(posted_at=posted, first_seen_at=first_seen))["Posted"] == shown


def test_a_long_location_list_is_shortened_not_cut():
    cities = ", ".join(["Cluj-Napoca", "Iași", "Sibiu", "Brașov", "Oradea", "Arad", "Deva"] * 4)
    value = _fields(_job(location=cities))["Location"]
    assert value.startswith("Cluj-Napoca, Iași, Sibiu") and value.endswith("more")
    assert len(value) <= 100


def test_the_description_is_cleaned():
    job = _job(description="  **Join us!**\n\n\n  We   build *things*\u200b\u00ad  " + "word " * 100)
    value = _fields(job)["Description"]
    assert "\n\n" not in value and "  " not in value
    assert "*" not in value and "\u200b" not in value and "\u00ad" not in value
    assert value.startswith("Join us! We build things")
    assert value.endswith("…") and len(value) <= 301
    # Nothing left: no empty field (Discord refuses the whole message).
    assert "Description" not in _fields(_job(description="   \n  "))


@pytest.mark.parametrize("title, description, starts", [
    # Production DMs opened with the company's "About us" or the title again.
    ("Junior IT Support",
     "**About MindMaze Therapeutics**\nMindMaze Therapeutics, a publicly listed scale-up.\n\n"
     "**What you'll do**\n- Support 200 users on Windows and macOS\n- Manage Jira tickets",
     "Support 200 users on Windows and macOS"),
    ("Internship - Local IT - Lugoj",
     "**Internship - Local IT - Lugoj**\nLocation Lugoj, Romania\n\n"
     "FORVIA HELLA is a listed international automotive supplier.\n\n"
     "Responsabilități:\nVei administra rețeaua locală și serverele.",
     "Vei administra rețeaua locală și serverele"),
    ("QA Engineer",
     "Despre noi: suntem o firmă de software din Cluj.\n\nTestezi aplicații web și mobile.",
     "Testezi aplicații web și mobile"),
    ("Data Analyst", "You will build dashboards in Power BI.", "You will build dashboards"),
])
def test_the_excerpt_starts_with_the_role_not_the_boilerplate(title, description, starts):
    assert _fields(_job(title=title, description=description))["Description"].startswith(starts)


def test_a_bad_link_does_not_break_the_message():
    embed, _view = build_job_embed_with_buttons(_job(url="not a url"), 1)
    assert embed.url is None
    embed, _view = build_job_embed_with_buttons(_job(), 1)
    assert embed.url.startswith("https://")


def test_title_and_company_are_whitespace_clean():
    embed, _view = build_job_embed_with_buttons(
        _job(title="  QA \n Engineer  ", company="  Acme   SRL "), 1,
    )
    assert embed.title == "QA Engineer"
    assert {f.name: f.value for f in embed.fields}["Company"] == "Acme SRL"


def _apply_button(view) -> str | None:
    urls = [item.url for item in view.children if getattr(item, "url", None)]
    return urls[0] if urls else None


def test_the_apply_button_goes_through_the_click_redirect(monkeypatch):
    """gosha-health's click tracking (gosha/clicks.py): only the button is
    tracked, the title link stays the posting's own."""
    for name, value in (("SESSION_SECRET", "test-secret"), ("DISCORD_CLIENT_ID", "1"),
                        ("DISCORD_CLIENT_SECRET", "x"), ("PUBLIC_BASE_URL", "https://gosha.test")):
        monkeypatch.setenv(name, value)
    job = _job()
    embed, view = build_job_embed_with_buttons(job, 42)
    assert _apply_button(view).startswith("https://gosha.test/api/v1/r/")
    assert embed.url == job.url

    monkeypatch.delenv("PUBLIC_BASE_URL")
    _embed, view = build_job_embed_with_buttons(job, 42)
    assert _apply_button(view) == job.url


def test_the_label_follows_the_score_scale():
    """Without a CV the stored score is a DM score ("Strong match" from
    0.47); with one it is the blended cosine (from 0.60)."""
    def label(**kw):
        embed, _view = build_job_embed_with_buttons(_job(), 1, 0.5, **kw)
        return embed.footer.text

    assert "Strong match" in label()
    assert "Good match" in label(scored_with_cv=True)
