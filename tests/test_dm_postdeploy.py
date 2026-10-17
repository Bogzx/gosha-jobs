"""The first production cycle after release 03e5314 (2026-10-16 17:35 UTC):
65 DMs judged by hand, 14 wrong. Each case below is a real posting from
that cycle, with its own title and text.

A  descriptions written for experienced people went to junior searches;
B  unmarked titles with no description went to junior searches on no evidence;
C  AI-training gig marketplaces (Alignerr) are not jobs;
D  a plant's line-engineering working student reached CS internship searches;
E  49 rows queued under the old gates were waiting to be sent as they were.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

import gosha.pipeline as pipeline
from gosha.filters import (
    asks_experienced,
    is_gig_marketplace,
    lacks_level_evidence,
    matches_experience_level,
    required_years,
    title_is_relevant,
)
from gosha.models import Job, Subscription, User, UserJob

# ── A: "experienced" in words ─────────────────────────────────────────

NTT_JAVA = (
    "For one of our clients, we are looking for an experienced Software Developer with "
    "strong Delphi knowledge and Java 17 skills to join a team responsible for the "
    "development and modernization of applications in the area of Dienstreisemanagement "
    "(Business Travel Management)."
)
NTT_SYSTEM = (
    "We're looking for an experienced and motivated System Engineer to support the "
    "development of complex automotive systems and products. In this role, you will "
    "contribute to system definition, requirements management, architecture development, "
    "technical coordination, and product integration throughout the development lifecycle."
)
SUPPORTYOURAPP = (
    "**What you need to succeed in this role:** * **Excellent English skills (at least C1 "
    "for both spoken and written);** * Experience in **Technical Support, SRE, or "
    "Engineering**, with strong troubleshooting and code-reading skills; * Proven "
    "customer-facing experience in a technical support or customer support role; * Strong "
    "**SQL skills** and experience with production-scale relational databases;"
)
VERTIV = (
    "We are looking for a motivated **Digital Tools & Configuration Developer** to join our "
    "EMEA Technology Application and Market Development team. This is a fantastic "
    "opportunity for someone with a background in software, IT, or engineering who enjoys "
    "solving practical business problems through technology. * Participate in architecture "
    "and solution review sessions alongside experienced colleagues. * We welcome "
    "applications from recent graduates and career changers with relevant technical skills. "
    "* Some experience with a programming or scripting language — JavaScript, Python, Java, "
    "or similar (even from study or personal projects)."
)


RWS = (
    "**Skills & Experience**\n\n* Previous experience working in localization engineering or "
    "with localization processes\n* Basic programming skills (especially Python)\n* Knowledge "
    "of regular expressions (Regex)\n* Familiarity with version control systems (e.g., Git)\n"
    "* Knowledge of additional localization file formats (e.g., JSON, YAML, XLIFF, RESX)\n* "
    "Familiarity with CAT tools and localization technologies\n\nIn accordance with "
    "applicable pay transparency legislation, candidates will be informed of the salary range "
    "for this position prior to interview. Pay is determined without reference to previous "
    "salary history and is aligned to objective job \u2011 related criteria."
)


@pytest.mark.parametrize("title, label, description", [
    ("Java Software Developer (strong Delphi and German)", None, NTT_JAVA),
    # Judged wrong in the pre-redeploy review of would_send_v3 (subs 3/5/25/26).
    ("Software Localization Engineer", None, RWS),
    ("System Engineer", None, NTT_SYSTEM),
    ("(Fluent English) T3 Support Engineer (EU, Remote)", None, SUPPORTYOURAPP),
    # The board's "entry" label does not outrank the text.
    ("System Engineer", "entry", NTT_SYSTEM),
])
def test_a_posting_for_experienced_people_is_not_junior(title, label, description):
    assert asks_experienced(description)
    assert not matches_experience_level(title, ["junior"], label, description)
    assert not title_is_relevant(title, "cs entry level", label, description, ["junior"])
    assert matches_experience_level(title, ["mid"], label, description)


def test_experienced_colleagues_and_some_experience_stay_junior():
    title = "Digital Tools & Configuration Developer"
    assert not asks_experienced(VERTIV)
    assert matches_experience_level(title, ["junior"], "entry", VERTIV)
    assert title_is_relevant(title, "cs entry level", "entry", VERTIV, ["junior"])


@pytest.mark.parametrize("text, experienced", [
    ("Join our experienced and motivated team of developers.", False),
    ("Proven experience with Docker is a plus.", False),
    ("Preferred qualifications: proven experience in backend development.", False),
    ("Requirements: proven experience working with Excel and SQL.", False),  # tools
    ("We are not looking for an experienced developer: we train you.", False),
    ("Requirements: proven experience as a frontend developer.", True),
    ("Căutăm un programator experimentat în Java.", True),
    ("* Previous experience as a QA engineer", True),
    ("Previous experience with Python is a plus.", False),
    ("Previous experience in Java is nice to have.", False),
    ("Preferred: previous experience with React.", False),
    ("No previous experience required: we train you.", False),
    ("Previous experience in web development is not required.", False),
    ("Some experience with a programming or scripting language (even from university "
     "projects).", False),
    ("Pay is determined without reference to previous salary history.", False),
    # Perficient's associate QA posting (prod, 2026-10-16): a wish.
    ("* Previous experience in a regulated industry, such as medical devices or "
     "pharmaceuticals, would be highly valuable.", False),
])
def test_experienced_wording(text, experienced):
    assert asks_experienced(text) is experienced


def test_years_written_4y_plus():
    """NTT DATA's Java posting also asked for "At least 4y+ of experience"."""
    assert required_years("* At least 4y+ of experience in working with Java 17") == 4
    assert required_years("Experience: 2y+ of experience with React") == 2
    assert required_years("Our 4y roadmap") is None


# Wrong DMs of the 22:08 UTC cycle (2026-10-16) to intern+junior searches.
METEOCONTROL = (
    "#### **Your profile:**\n\n* 3+ years as DevOps/Database engineer\n* TimeScaleDB or "
    "comparable time-series database\n* Database lifecycle management and performance "
    "optimizations\n* Podman and container orchestration\n* Backup and recovery concepts\n"
    "* CI/CD and build automation\n* Experience in agile software development\n* Good "
    "communication skills\n* Hands-on in industrial cybersecurity\n* Bonus: Experience with "
    "SCADA or comparable industrial systems"
)
NTT_AI_EXPERTS = (
    "We are looking for Artificial Intelligence Engineering Experts to lead the design, "
    "industrialization, and continuous improvement of production AI capabilities for our "
    "platform."
)
NTT_AI_TRACK_RECORD = (
    "* Proven expert-level track record designing, delivering, and operating production "
    "machine-learning or generative-AI systems"
)
# NTT DATA's QA Engineer, judged borderline: stays junior.
NTT_QA = (
    "The role requires strong test automation experience, ideally with Playwright, as well "
    "as the ability to work with SQL to validate data and investigate application behavior "
    "at database level.\n\n**What you'll bring along**\n\n* Degree in Computer Science, "
    "Business Informatics, or a comparable field\n* Hands-on experience developing and "
    "maintaining UI and REST API test automation using Playwright or Cypress\n* Experience "
    "in manual software testing, preferably within Agile environments (e.g., Scrum)"
)


@pytest.mark.parametrize("title, description", [
    ("Database & DevOps Engineer (all genders)", METEOCONTROL),
    ("AI Engineer (Python)", NTT_AI_EXPERTS),
    ("AI Engineer (Python)", NTT_AI_TRACK_RECORD),
])
def test_22_08_cycle_postings_are_not_junior(title, description):
    assert not matches_experience_level(title, ["intern", "junior"], None, description)
    assert not title_is_relevant(
        title, "computer science internship", None, description, ["intern", "junior"]
    )


def test_years_as_a_role():
    assert required_years("* 3+ years as DevOps/Database engineer") == 3
    assert required_years("* Minimum 3-6+ years as a Platform Engineer") == 3
    assert required_years("Ai lucrat 2 ani ca programator") == 2
    assert required_years("Experience in Java, 5 years as a Senior Java Developer") == 5
    # "as" without a role: not experience.
    assert required_years("You will spend 2 years as part of our rotation programme") is None
    assert required_years("2 years as a student assistant") is None
    # A wish stays a wish.
    assert required_years("Bonus: 3 years as a data engineer") is None


@pytest.mark.parametrize("text, experienced", [
    (NTT_AI_EXPERTS, True),
    (NTT_AI_TRACK_RECORD, True),
    ("We are seeking cloud security experts to join the team.", True),
    ("We are looking for students who want to become experts in cloud.", False),
    ("We are looking for future experts in data engineering.", False),
    ("Our experts will mentor you.", False),
    ("A proven academic track record is a plus.", False),
])
def test_experts_and_track_record_wording(text, experienced):
    assert asks_experienced(text) is experienced


def test_borderline_ntt_qa_stays_junior():
    title = "QA Engineer with SQL/Database Testing"
    assert required_years(NTT_QA) is None
    assert not asks_experienced(NTT_QA)
    assert matches_experience_level(title, ["intern", "junior"], None, NTT_QA)


# ── B: no evidence of the level ───────────────────────────────────────

LONG_TEXT = "You will build data pipelines in Python and SQL with our team in Cluj. " * 4


def _job(title, description="", company="Bosch Romania", source="hipo", label=None, **kw):
    fields = dict(
        url=f"https://b.test/{abs(hash((title, company, description)))}", title=title,
        company=company, location="Cluj-Napoca, Romania", source=source,
        description=description, experience_level=label,
        first_seen_at=datetime.now(timezone.utc),
    )
    fields.update(kw)
    return Job(**fields)


def _search(levels, keywords=("software engineer",), locations=("cluj",)):
    sub = Subscription(user_id=1, max_age_days=7)
    sub.keywords, sub.locations, sub.experience_levels = list(keywords), list(locations), levels
    return sub


@pytest.mark.parametrize("title, company", [
    ("AI/ML Engineer", "Bosch Romania"),
    ("Data Engineer & Digital Transformation Developer (m/f/d)", "AUMOVIO Romania"),
    ("IT SAP BTP Developer (m/f/d)", "AUMOVIO Romania"),
])
def test_an_unmarked_title_without_a_description_is_not_sent_to_a_junior_search(title, company):
    for description in ("", "Location: Cluj-Napoca. Apply now."):  # empty, or under 200 chars
        assert lacks_level_evidence(title, None, description)
        job = _job(title, description, company)
        assert not pipeline.prefilter(_search(["junior"]), [job])
        assert not pipeline.prefilter(_search(["intern", "junior"]), [job])
    # With a description to read, it is judged on it.
    assert pipeline.prefilter(_search(["junior"]), [_job(title, LONG_TEXT, company)])
    # A search that takes any level does not need it.
    assert pipeline.prefilter(_search(["any"]), [_job(title, "", company)])


def test_a_training_title_needs_no_description():
    title = "Working Student in Embedded Software Development - High Level"
    assert not lacks_level_evidence(title, None, "")
    for levels in (["intern"], ["intern", "junior"]):
        assert pipeline.prefilter(
            _search(levels, keywords=("computer science intern",)), [_job(title)],
        )


# ── C: AI-training gig marketplaces ───────────────────────────────────

@pytest.mark.parametrize("company", [
    "Alignerr", "Outlier", "Outlier AI", "DataAnnotation", "DataAnnotation.tech", "Remotasks",
    "Mercor", "Toloka", "Toloka AI", "Appen", "TELUS International AI",
    "TELUS Digital AI Data Solutions",
])
def test_gig_marketplaces(company):
    assert is_gig_marketplace(company)


@pytest.mark.parametrize("company", ["Bosch Romania", "Outlier Ventures", "TELUS", "Endava", None])
def test_employers_are_not_gig_marketplaces(company):
    assert not is_gig_marketplace(company)


@pytest.mark.parametrize("title", [
    "Software Engineer (AI Training)", "Back End Developer (AI Infrastructure)",
])
def test_an_alignerr_posting_is_never_dmed(title):
    job = _job(title, LONG_TEXT, "Alignerr", source="linkedin", label="entry")
    for levels in (["junior"], ["any"]):
        assert not pipeline.prefilter(_search(levels), [job])


# ── D: not CS behind "Engineering" ────────────────────────────────────

LINE_ENGINEERING = (
    "Your tasks Growth opportunity, joining various team projects, in order to improve your "
    "technical skills;\nFamiliarize with the equipments and the processes from our plant;\n"
    "Apply your theoretical knowledge from university in practice, being involved in "
    "production projects;\nManage tasks like, data input and data-bases up-dates, depending "
    "on what you are passionate about;\nYour profile Currently studying in a technical field "
    "(Mechatronics, Mechanical Engineering, Electrical Engineering, Industrial Engineering, "
    "Robotics and Production Management, Precision Engineering or similar);\nWe are looking "
    "for a medium level of Microsoft Office (Excel), because you will be responsible to "
    "process input data;\nInterest in learning about processes from a manufacturing factory;"
)
PRODUCTION_IT = (
    "Your tasks As a working student in the IT Production department you will have the "
    "opportunity to practice and increase you knowledge in the IT fields with a variety of "
    "subjects.\nIT infrastructure and MES system in Bosch Cluj.\nIT security in our "
    "manufacturing area."
)
PROCESS_DIGITALIZATION = (
    "Your tasks Analyze production data and transform it into actionable insights.\nDevelop "
    "Power BI dashboards and digital tools for performance monitoring.\nYour profile Student "
    "in Computer Science, Automation, Data Science, Industrial Engineering, or a related "
    "field.\nExperience with Excel; knowledge of Power BI, Power Apps, Power Automate, "
    "Python, SQL, or similar tools is a plus."
)


@pytest.mark.parametrize("title, description, relevant", [
    ("Working Student – Line Engineering Support", LINE_ENGINEERING, False),
    ("Working Student Production IT", PRODUCTION_IT, True),
    ("Working Student Process Engineering & Digitalization", PROCESS_DIGITALIZATION, True),
    # No description to read: kept (the scrape stage sees list pages).
    ("Working Student – Line Engineering Support", None, True),
])
def test_cs_keywords_need_cs_work(title, description, relevant):
    for keyword, levels in (("computer science intern", ["intern"]),
                            ("software engineer", ["intern", "junior"])):
        assert title_is_relevant(title, keyword, "entry", description, levels) is relevant
    # Not for a search that is not a CS one.
    assert title_is_relevant(title, "working student", "entry", description, ["intern"])


# ── E: the queue is judged again at send time ─────────────────────────


class _Bot:
    alert_channel_id = None

    def get_channel(self, _id):
        return None


async def _queued(session, specs):
    user = User(discord_user_id=7701)
    session.add(user)
    await session.flush()
    sub = Subscription(user_id=user.id, max_age_days=7)
    sub.keywords, sub.locations, sub.experience_levels = ["software engineer"], ["cluj"], ["junior"]
    session.add(sub)
    await session.flush()
    ids = []
    for i, (title, company, description) in enumerate(specs):
        job = _job(title, description, company, source="linkedin",
                   url=f"https://e.test/{i}")
        session.add(job)
        await session.flush()
        session.add(UserJob(user_id=user.id, job_id=job.id, subscription_id=sub.id,
                            relevance_score=0.40 - i / 100, dm_status=UserJob.DM_PENDING))
        ids.append(job.id)
    await session.commit()
    return ids


async def _statuses(session) -> dict[int, str]:
    session.expire_all()
    return {uj.job_id: uj.dm_status for uj in (await session.execute(select(UserJob))).scalars()}


@pytest.mark.asyncio
async def test_rows_queued_under_old_gates_are_rechecked_at_send_time(
    patched_db, session, monkeypatch,
):
    ids = await _queued(session, [
        ("Software Engineer (AI Training)", "Alignerr", LONG_TEXT),
        ("Java Software Developer (strong Delphi and German)", "NTT DATA", NTT_JAVA + LONG_TEXT),
        ("AI/ML Engineer", "Bosch Romania", ""),
        ("Digital Tools & Configuration Developer", "Vertiv", VERTIV),
    ])
    sent: list[str] = []

    async def fake_dm(bot, discord_id, embed, view=None):
        sent.append(embed.title)
        return pipeline.DM_SENT

    monkeypatch.setattr(pipeline, "_dm_user", fake_dm)
    monkeypatch.setattr(pipeline, "_send_text", lambda *a: asyncio.sleep(0))

    assert await pipeline.deliver_pending(_Bot()) == 1
    assert sent == ["Digital Tools & Configuration Developer"]
    statuses = await _statuses(session)
    assert statuses[ids[0]] == "skipped" and statuses[ids[1]] == "skipped"
    # No description yet: waits, neither sent nor dropped.
    assert statuses[ids[2]] == "pending"

    # A later cycle brings its description: judged on it, and sent.
    job = await session.get(Job, ids[2])
    job.description = LONG_TEXT
    await session.commit()
    assert await pipeline.deliver_pending(_Bot()) == 1
    assert sent[-1] == "AI/ML Engineer"
