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

import gosha.filters as filters
import gosha.pipeline as pipeline
from gosha.filters import (
    asks_experienced,
    has_entry_evidence,
    is_not_a_job,
    is_gig_marketplace,
    lacks_level_evidence,
    matches_experience_level,
    required_years,
    split_glued_level,
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

LONG_TEXT = (
    "You will build data pipelines in Python and SQL with our team in Cluj. " * 4
    + "Recent graduates are welcome."
)


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


# ── Positive entry evidence for junior DMs (DM_JUNIOR_RULE) ───────────
# Wrong candidates in gosha-label's audit (prod snapshot 2026-10-17 10:48):
# demanding text that no years/"experienced" pattern reads.
AUDIT_WRONG = [
    ("JAVA Developer with French", "* Strong experience with Java 8+"),
    ("AI Engineer", "* Several years of professional experience in AI development, solution "
     "architecture, particularly on Azure, with working knowledge of comparable services on "
     "AWS and GCP"),
    ("AI Engineer, Employee Products", "* Have 4–8 years in software, data, or AI/ML "
     "engineering and have shipped production AI systems, not prototypes."),
]
FILLER = " You will work with our team in Cluj on internal tools and services." * 4


@pytest.fixture
def evidence_rule(monkeypatch):
    """Pins the rule, so these tests hold after a rollback to "default"."""
    monkeypatch.setattr(filters, "DM_JUNIOR_RULE", "evidence")


def test_the_dm_junior_rule_ships_evidence_based():
    """Fails closed (docs/EVAL.md, 2026-10-17); "default" is the rollback."""
    assert filters.DM_JUNIOR_RULE == "evidence"
    assert filters.DM_ENTRY_LABEL_IS_EVIDENCE


@pytest.mark.parametrize("title, sentence", AUDIT_WRONG)
def test_no_entry_evidence_no_junior_dm(title, sentence, evidence_rule, monkeypatch):
    description = sentence + FILLER
    job = _job(title, description)
    assert not has_entry_evidence(title, None, description)
    assert not pipeline.prefilter(_search(["junior"]), [job])
    assert not pipeline.prefilter(_search(["intern", "junior"]), [job])
    # Not an entry-level search: unaffected.
    assert pipeline.prefilter(_search(["any"]), [job])
    # The switch restores the old default.
    monkeypatch.setattr(filters, "DM_JUNIOR_RULE", "default")
    assert pipeline.prefilter(_search(["junior"]), [job])


@pytest.mark.parametrize("title, text", [
    ("Junior Python Developer", ""),
    ("Software Engineer Intern", ""),
    ("Accounting Assistant RO", "Required Skills Basic knowledge of accounting principles."),
    ("Data Analyst", "* 1-2 years of experience with SQL"),
    ("HR Specialist", "Experiență anterioară în IT Sourcing (minim 1-2 ani reprezintă un avantaj "
     "clar)."),
    ("Data Analyst", "Experience: 0-2 years."),
    ("Customer Support", "No prior experience required, we will train you."),
    ("Consultant Vânzări", "Nu este necesară experiență, oferim training."),
    ("QA Engineer", "Recent graduates are welcome to apply."),
    ("Customer Support", "A great place to start your career in tech."),
    ("Embedded Engineer", "Open to students in their final year."),
])
def test_entry_evidence(title, text):
    assert has_entry_evidence(title, None, text + FILLER)


@pytest.mark.parametrize("text", [
    "You will mentor junior engineers and review their code.",
    "Students will not be considered for this role.",
    "* 3+ years of experience with Java",
])
def test_not_entry_evidence(text):
    assert not has_entry_evidence("Software Engineer", None, text + FILLER)


def test_entry_label_needs_the_years_to_agree():
    assert has_entry_evidence("QA Engineer", "entry", FILLER)
    assert not has_entry_evidence("QA Engineer", "entry", "* 4+ years of experience." + FILLER)


def test_intern_only_searches_are_unchanged(evidence_rule):
    job = _job("Software Engineer Intern", FILLER)
    assert pipeline.prefilter(_search(["intern"]), [job])


# ── Not a job: courses and talent-network CV drops (audit cause E) ────
ENTERSOFTONE = (
    "Învață una dintre cele mai căutate specializări la intersecția dintre tehnologie și "
    "business\nAcesta nu este un anunț de angajare, ci un program de training organizat de "
    "ENTERSOFTONE Training Academy.\nPe parcursul cursului vei învăța cum sunt analizate "
    "procesele unei companii și cum sunt transpuse într-o soluție ERP.\nÎnscrie-te până la "
    "23 octombrie 2026"
)
SCHAEFFLER_TALENT = (
    "General informationWe are always on the lookout for exceptional individuals to join our "
    "team. If you didnt identify a suitable open position, you can submit your CV here and "
    "well consider you for future opportunities that match your skills and experience."
)
# Real internships whose text sounds like the above.
QUALCOMM = (
    "By applying here, you are expressing interest in one of our many **2027 Qualcomm IT "
    "Infrastructure & Cloud internships** . It is important to note that this is not a job "
    "posting for a specific role. We will review resumes on an ongoing basis, and a recruiter "
    "may reach out to you."
)
ARCH = (
    "Simply create an account and opt in to receive emails when we have job openings that meet "
    "your criteria. Join our talent community to share your preferences directly with Arch’s "
    "Talent Acquisition team."
)
AUTOZONE = (
    "If you are uncertain which department is the best fit, please use this general "
    "application. * Applications close on Friday, December 4, 2026."
)


@pytest.mark.parametrize("title, description", [
    ("Curs Consultant ERP ENTERSOFTONE Training Academy", ENTERSOFTONE),
    ("Join Our Talent Network: Your Electrified Future Starts Here!", SCHAEFFLER_TALENT),
    ("Data Analytics Bootcamp", "A 12-week bootcamp in SQL and Power BI. Enroll now, seats "
     "are limited."),
    ("Python Course", "Course fee: 1,200 RON, paid in two instalments."),
    ("Curs Java", "Cursul durează 3 luni. Taxa de participare este 2000 lei."),
    ("Join our Talent Community", "Leave us your details and we will contact you about "
     "future opportunities."),
])
def test_not_a_job(title, description):
    assert is_not_a_job(title, description)
    job = _job(title, description + FILLER)
    assert not pipeline.prefilter(_search(["intern", "junior"]), [job])
    assert not pipeline.prefilter(_search(["any"]), [job])


@pytest.mark.parametrize("title, description", [
    ("IT Infrastructure & Cloud Engineering Internship – Summer 2027", QUALCOMM),
    ("Security Operations Intern", ARCH),
    ("AutoZone 2027 Summer Internship – Data Science", AUTOZONE),
    # Title words alone do not decide.
    ("Software Engineering Academy Intern", "You will build internal tools in Java."),
    ("Talent Acquisition Intern", "Support recruiters in finding candidates for future "
     "openings."),
    # A student requirement and courses offered as a benefit.
    ("Software Engineer Intern", "Students currently enrolled in a Computer Science course. "
     "We offer language courses and a training budget."),
    ("Junior Developer", "Oferim cursuri de limba germana si training intern."),
    ("Junior Developer", None),
])
def test_a_job_is_not_flagged(title, description):
    assert not is_not_a_job(title, description)


# ── V3 evidence classes (main eval set's "any" postings, 2026-10-17) ──
@pytest.mark.parametrize("title, text", [
    # Experience not needed, in other words.
    ("Sofer Curier", "nu aveți nevoie de experiență anterioară și nici de cunoștințe de limbi "
     "străine"),
    ("Consultant Credite", "Training complet (nu ai nevoie de experiență)"),
    ("Support Agent", "You don't need prior experience."),
    # With or without it.
    ("Appointment Setter", "te învățăm tot ce trebuie să știi ... chiar dacă ai sau nu "
     "experiență anterioară similară"),
    ("Agent Vanzari", "Angajăm cu sau fără experiență."),
    # An advantage but not required.
    ("Sales Partner", "Experiența în sales reprezintă un avantaj, dar nu este obligatorie"),
    ("Support Agent", "Experience in customer service is an advantage, but not required."),
    # Training that teaches the job.
    ("L1 Service Technician", "Technical college or university is not mandatory as necessary "
     "training will be ensured"),
    ("Controlor financiar", "O perioadă de training în care vei învăța tot ce ai nevoie"),
    # A new trade.
    ("Site QAIT", "Oportunitatea de a învăța și de te califica intr-o noua meserie"),
    ("Data Analyst", "We welcome career changers."),
    # A school-leaving minimum.
    ("Consultant Credite", "Cerinta minima diploma de bacalaureat"),
    # Level one of a ladder, in the title or the text.
    ("Technical Analyst I", ""),
    ("Application Support / SQL", "As a Technical Analyst I, you will troubleshoot"),
    ("Customer Support", "As a Level 1 Customer Support Consultant you will"),
])
def test_v3_entry_evidence(title, text):
    assert has_entry_evidence(title, None, text + FILLER)


@pytest.mark.parametrize("title, text", [
    ("Technical Analyst II", ""),
    ("Software Engineer", "Training will be provided on our internal tools."),
    ("Software Engineer", "Experience with Kafka is required."),
    ("Software Engineer", "As a team, I think we build great things."),
    ("Accountant", "Studii superioare economice."),
])
def test_v3_not_entry_evidence(title, text):
    assert not has_entry_evidence(title, None, text + FILLER)


def test_entry_label_evidence_can_be_switched_off(monkeypatch):
    assert has_entry_evidence("QA Engineer", "entry", FILLER)
    monkeypatch.setattr(filters, "DM_ENTRY_LABEL_IS_EVIDENCE", False)
    assert not has_entry_evidence("QA Engineer", "entry", FILLER)


# ── Glued level words ("InternSoftware Engineer", Ascom) ──────────────
@pytest.mark.parametrize("title, split", [
    ("InternSoftware Engineer", "Intern Software Engineer"),
    ("JuniorDeveloper", "Junior Developer"),
    ("SeniorData Engineer", "Senior Data Engineer"),
    ("Internal Auditor", "Internal Auditor"),
    ("DevOps Engineer", "DevOps Engineer"),
    ("JavaScript Developer", "JavaScript Developer"),
])
def test_glued_level_words_are_split(title, split):
    assert split_glued_level(title) == split


def test_a_glued_intern_title_is_an_internship():
    title = "InternSoftware Engineer"
    assert matches_experience_level(title, ["intern"])
    assert title_is_relevant(title, "computer science internship", None, FILLER, ["intern"])
    assert has_entry_evidence(title, None, FILLER)
    assert not matches_experience_level("SeniorData Engineer", ["junior"])


# ── Months ("min. 24 luni", OKKWEBMEDIA's Wordpress developer, 8319) ───
# Unparsed, the posting stated no years, so under V3 it lacked evidence
# and left junior searches; 24 months is two years, which is junior.
WORDPRESS = (
    "Căutăm Wordpress developer la sediul nostru din Cluj-Napoca/remote!\n\n"
    "ℹ Cerințe:\n"
    "✅ să ai cunoștințe bune de Wordpress, PHP, SQL, Javascript și CSS\n"
    "✅ să fii atent la detalii\n"
    "✅ să ai experienta de min. 24 luni\n\n"
    "☝ Constituie avantaj:\n"
    "🚩 experiență dezvoltare pluginuri Wordpress\n"
)


def test_months_of_experience_count_as_years():
    assert required_years(WORDPRESS) == 2
    assert filters.dm_junior_ok("Wordpress developer", None, WORDPRESS + FILLER)


@pytest.mark.parametrize("text, years", [
    ("Minim 18 luni experiență în programare.", 2),
    ("At least 30 months of experience as a developer.", 3),
    ("Experience: 36 months.", 3),
    ("6 months of practical experience.", 1),
    ("A 6 month internship in our team.", None),
    ("18 months of experience is a plus.", None),
])
def test_months_round_up_to_years(text, years):
    assert required_years(text) == years


def test_more_than_24_months_is_not_junior():
    text = "At least 30 months of experience as a developer." + FILLER
    assert not matches_experience_level("PHP Developer", ["junior"], None, text)


def test_months_in_a_title_are_a_duration_not_a_requirement():
    text = "5+ years of experience with Java." + FILLER
    assert not matches_experience_level("Java Developer (12 month contract)", ["junior"], None, text)


# ── Graduate hire by grade (The Access Group "Software Engineer", 8294) ─
# Its experience is all "might also include", no years; what it asks for
# is a degree with a minimum graduation grade, which is a graduate hire.
ACCESS = (
    "About you:\n\n"
    "You’re someone who enjoys building modern web applications and cares about "
    "performance, user experience, and delivering value. You’re curious, collaborative, "
    "and enjoy working in a fast-paced environment where you can learn and grow.\n\n"
    "Your skills and experiences might also include:\n\n"
    "* Experience building modern web applications and working across frontend and backend\n"
    "* Exposure to cloud platforms (such as AWS, Azure or GCP) and Infrastructure as Code tools\n\n"
    "Note: Candidates must hold a completed university degree and have achieved a minimum "
    "graduation grade of\n**9/10**\nto be considered for this position.\n"
)


def test_a_minimum_graduation_grade_is_entry_evidence():
    assert has_entry_evidence("Software Engineer", None, ACCESS + FILLER)
    assert filters.dm_junior_ok("Software Engineer", None, ACCESS + FILLER)
    assert matches_experience_level("Software Engineer", ["junior"], None, ACCESS + FILLER)


@pytest.mark.parametrize("text", [
    "Minimum GPA of 3.5 required.",
    "Media de absolvire minim 8.",
])
def test_grade_requirements_in_other_words(text):
    assert has_entry_evidence("Software Engineer", None, text + FILLER)


@pytest.mark.parametrize("text", [
    # Years asked: the grade does not make it a graduate hire.
    ACCESS + "3+ years of professional experience with TypeScript.",
    # A degree alone, no grade.
    "Candidates must hold a completed university degree in Computer Science.",
])
def test_not_graduate_hire_evidence(text):
    assert not has_entry_evidence("Software Engineer", None, text + FILLER)


# ── "Basic knowledge/understanding", "eager to learn": not evidence alone ─
# Postings for experienced people write them beside the experience they
# demand. Siemens "SRM Backend & Tools Engineer" (8266, sent to a junior
# search) and NTT DATA "QA Engineer with SQL/Database Testing" (8380).
SIEMENS_SRM = (
    "Required at Onboarding:\n\n"
    "* Oracle database administration experience (operations, maintenance, troubleshooting)\n"
    "* Strong SQL skills (maintenance tasks, error analysis)\n"
    "* Experience with at least one scripting language (Python, Perl or comparable – Tcl/Tk "
    "will be trained on the job)\n"
    "* Basic understanding of system integration & interface architectures (SAP, PLM tools, "
    "file transfer)\n"
    "* Tcl/Tk experience\n"
)
NTT_QA = (
    "* Hands-on experience developing and maintaining UI and REST API test automation using "
    "Playwright or Cypress\n"
    "* Experience in manual software testing, preferably within Agile environments (e.g., Scrum)\n"
    "* Experience working with test management tools such as Jira and Xray\n"
    "* Basic understanding of the software development lifecycle (SDLC)\n"
)


@pytest.mark.parametrize("title, text", [
    ("SRM Backend & Tools Engineer", SIEMENS_SRM),
    ("QA Engineer with SQL/Database Testing", NTT_QA),
    ("Software Engineer", "Eager to learn new technologies."),
    ("Software Engineer", "Cunoștințe de bază de Java."),
])
def test_basic_knowledge_is_not_entry_evidence(title, text):
    assert not has_entry_evidence(title, None, text + FILLER)


def test_basic_knowledge_beside_real_evidence_still_passes():
    text = "Basic knowledge of Java. Students and recent graduates are welcome." + FILLER
    assert has_entry_evidence("Software Engineer", None, text)


# ── Internship and junior are different levels (owner, 2026-10-17) ───────
# A training position open to students is an internship; one that needs a
# finished degree is junior. Real titles and sentences from sent DMs.
TENARIS = (
    "This role is part of the Global Trainee Program (GTP) – a unique opportunity for recent "
    "graduates to gain practical experience and receive structured training over a 2-year "
    "period.\n\n"
    "* Bachelor's degree in Electrical Engineering | Automation Engineering | Metallurgy | "
    "Material Science | Industrial Engineering | Mechanical Engineering or related fields\n"
)
INTEL_GRADUATE_INTERN = (
    "* Currently pursuing a Master’s degree in Computer Science, Computer Engineering, Electrical "
    "Engineering, Applied Mathematics, or a related technical field (able to keep student status "
    "for at least 1 year).\n"
)
YONDER_TRAINEE = (
    "* Having Technical Studies: currently pursuing or a graduate of a Bachelor's Degree and/or "
    "a Master's Degree\n"
)
IT_INTERN_GRADUATE = (
    "Recently completed a degree in IT, Computer Science, Information Systems, or a related "
    "field.\n"
)


@pytest.mark.parametrize("title, board_level, text, level", [
    # Recent graduates, not students: a graduate hire, whatever "Trainee" says.
    ("Junior Engineer (Global Trainee) | Zalau", "entry", TENARIS, "junior"),
    # A graduate programme by its title.
    ("Vodafone Romania Discover Graduate IT-AI Engineering Programme, 2027", None, "", "junior"),
    # An internship that asks for a finished degree.
    ("IT Intern - 6 months FTC", "entry", IT_INTERN_GRADUATE, "junior"),
    # "Graduate Intern" for master's students: an internship, and only that.
    ("MLIR Compiler Engineering Graduate Intern (f/m/d)", None, INTEL_GRADUATE_INTERN, "intern"),
    # Open to people still studying: a training position, not a junior role.
    ("On-the-job Trainee Program, Junior Developer, Cluj-Napoca", "entry", YONDER_TRAINEE, "intern"),
    # "Recent graduates are welcome" does not make an internship a junior role.
    ("Software Engineer Intern", None, "Recent graduates are welcome.", "intern"),
])
def test_intern_and_junior_are_different_levels(title, board_level, text, level):
    text += FILLER
    assert filters.matched_level(title, ["intern", "junior"], board_level, text) == level
    assert matches_experience_level(title, ["intern"], board_level, text) == (level == "intern")
    assert matches_experience_level(title, ["junior"], board_level, text) == (level == "junior")


def test_an_intern_only_search_does_not_get_a_graduate_trainee_programme():
    title, text = "Junior Engineer (Global Trainee) | Zalau", TENARIS + FILLER
    assert not title_is_relevant(title, "computer science internship", "entry", text, ["intern"])


@pytest.mark.parametrize("levels, level", [
    (["intern"], "intern"), (["junior"], None), (["any"], None), (["mid"], None), ([], None),
])
def test_matched_level_names_the_level_the_search_took(levels, level):
    assert filters.matched_level("Software Engineering Internship", levels, None, FILLER) == level


def test_matched_level_for_a_junior_role():
    assert filters.matched_level("Junior Java Developer", ["intern", "junior"], None, FILLER) == "junior"
    assert filters.matched_level("Junior Java Developer", ["intern"], None, FILLER) is None
