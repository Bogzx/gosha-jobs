"""Tests for cross-board job deduplication."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gosha import dedup
from gosha.models import Job


def test_normalize_key_ignores_case_punctuation_and_legal_suffixes():
    a = dedup.normalize_key("Senior Python Developer!", "Acme S.R.L.")
    b = dedup.normalize_key("senior python developer", "ACME SRL")
    assert a == b

    c = dedup.normalize_key("QA Engineer", "Other Corp")
    assert a != c


def test_normalize_key_ignores_decorations_and_word_order():
    base = dedup.normalize_key("Senior Python Developer", "Acme")
    assert dedup.normalize_key("Senior Python Developer (Remote)", "Acme") == base
    assert dedup.normalize_key("Python Developer Senior [Hybrid]", "Acme") == base
    assert dedup.normalize_key("Senior Python Developer - Cluj-Napoca", "Acme") == base
    # A genuinely different seniority stays distinct
    assert dedup.normalize_key("Junior Python Developer", "Acme") != base


@pytest.mark.asyncio
async def test_assign_groups_marks_duplicates(patched_db, session):
    j1 = Job(url="https://indeed.com/x", title="Python Dev", company="Acme", source="indeed")
    j2 = Job(url="https://linkedin.com/x", title="Python Dev", company="Acme", source="linkedin")
    j3 = Job(url="https://ejobs.ro/x", title="python  dev", company="ACME", source="ejobs")
    unique = Job(url="https://indeed.com/y", title="QA", company="Other", source="indeed")
    session.add_all([j1, j2, j3, unique])
    await session.commit()

    marked = await dedup.assign_dedup_groups()
    assert marked == 2  # two non-canonical duplicates

    for job in (j1, j2, j3, unique):
        await session.refresh(job)
    canonical = min(j1.id, j2.id, j3.id)
    assert j1.dedup_group_id == canonical
    assert j2.dedup_group_id == canonical
    assert j3.dedup_group_id == canonical
    assert unique.dedup_group_id is None or unique.dedup_group_id == unique.id


@pytest.mark.asyncio
async def test_assign_groups_skips_old_and_inactive(patched_db, session):
    old = Job(url="https://a.com/1", title="Same", company="Co", source="indeed")
    old.first_seen_at = datetime.now(timezone.utc) - timedelta(days=60)
    inactive = Job(
        url="https://a.com/2", title="Same", company="Co", source="linkedin",
        is_active=False,
    )
    fresh = Job(url="https://a.com/3", title="Same", company="Co", source="ejobs")
    session.add_all([old, inactive, fresh])
    await session.commit()

    marked = await dedup.assign_dedup_groups()
    assert marked == 0  # only one in-window active job with that key

    await session.refresh(fresh)
    assert fresh.dedup_group_id in (None, fresh.id)


@pytest.mark.asyncio
async def test_browse_hides_non_canonical_duplicates(patched_db, session, web_env_unused=None):
    from gosha.services import jobs as jobs_service

    j1 = Job(url="https://indeed.com/d", title="Dup Dev", company="Acme", source="indeed")
    j2 = Job(url="https://linkedin.com/d", title="Dup Dev", company="Acme", source="linkedin")
    session.add_all([j1, j2])
    await session.commit()
    await dedup.assign_dedup_groups()

    page, total = await jobs_service.list_jobs(
        user_id=1, filters=jobs_service.JobFilters(), page=1, per_page=50,
    )
    urls = {j.url for j in page}
    assert total == 1
    assert urls == {"https://indeed.com/d"}  # canonical (lowest id) survives


@pytest.mark.asyncio
async def test_feed_hides_non_canonical_duplicates(patched_db, session):
    import numpy as np

    from gosha import recommend
    from gosha.embeddings import EMBEDDING_DIM, vec_to_bytes
    from gosha.models import User

    vec = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    vec[0] = 1.0
    user = User(discord_user_id=606, cv_embedding=vec_to_bytes(vec))
    session.add(user)
    j1 = Job(
        url="https://indeed.com/f", title="Feed Dup", company="Acme",
        source="indeed", embedding=vec_to_bytes(vec),
    )
    j2 = Job(
        url="https://linkedin.com/f", title="Feed Dup", company="Acme",
        source="linkedin", embedding=vec_to_bytes(vec),
    )
    session.add_all([j1, j2])
    await session.commit()
    await dedup.assign_dedup_groups()

    items, total = await recommend.get_feed(user.id, page=1, per_page=10)
    assert total == 1
    assert [item.job.url for item in items] == ["https://indeed.com/f"]


def test_normalize_key_keeps_language_symbols():
    """"C++", "C#" and "C" are different jobs, not one after punctuation."""
    keys = {
        dedup.normalize_key(title, "Acme")
        for title in ("C++ Developer", "C# Developer", "C Developer")
    }
    assert len(keys) == 3
    assert dedup.normalize_key("Developer C++ (Remote)", "Acme") == dedup.normalize_key(
        "C++ Developer", "Acme",
    )
    # A hashtag is decoration, not part of the role.
    assert dedup.normalize_key("Python Developer #remote", "Acme") == dedup.normalize_key(
        "Python Developer", "Acme",
    )


def test_normalize_key_refuses_placeholders():
    """No employer, or no title, is not an identity: never group on it."""
    assert dedup.normalize_key("Python Developer", "Unknown") is None
    assert dedup.normalize_key("Python Developer", "") is None
    assert dedup.normalize_key("Python Developer", "N/A") is None
    assert dedup.normalize_key("Unknown Title", "Acme") is None
    assert dedup.normalize_key("Remote (m/f/d)", "Acme") is None
    assert dedup.normalize_key("Python Developer", "Acme") is not None


async def _group_ids(session, *jobs: Job) -> list[int | None]:
    for job in jobs:
        await session.refresh(job)
    return [job.dedup_group_id for job in jobs]


@pytest.mark.asyncio
async def test_unknown_employers_and_different_languages_not_merged(patched_db, session):
    session.add_all([
        Job(url="https://a.com/u1", title="Accountant", company="Unknown", source="indeed"),
        Job(url="https://b.com/u2", title="Accountant", company="Unknown", source="linkedin"),
        Job(url="https://a.com/c1", title="C++ Developer", company="Acme", source="indeed"),
        Job(url="https://a.com/c2", title="C# Developer", company="Acme", source="indeed"),
    ])
    await session.commit()

    assert await dedup.assign_dedup_groups() == 0


@pytest.mark.asyncio
async def test_lone_survivor_is_released_when_canonical_expires(patched_db, session):
    """The canonical's link died: the duplicate is now the only live copy and
    must show up again instead of pointing at a dead row forever."""
    j1 = Job(url="https://indeed.com/e", title="Go Dev", company="Acme", source="indeed")
    j2 = Job(url="https://ejobs.ro/e", title="Go Dev", company="Acme", source="ejobs")
    session.add_all([j1, j2])
    await session.commit()
    await dedup.assign_dedup_groups()
    assert await _group_ids(session, j2) == [j1.id]

    j1.is_active = False
    await session.commit()
    assert await dedup.assign_dedup_groups() == 0
    assert await _group_ids(session, j2) == [None]

    from gosha.services import jobs as jobs_service

    page, total = await jobs_service.list_jobs(
        user_id=1, filters=jobs_service.JobFilters(), page=1, per_page=50,
    )
    assert [job.url for job in page] == ["https://ejobs.ro/e"] and total == 1


@pytest.mark.asyncio
async def test_group_reelects_a_live_canonical(patched_db, session):
    j1, j2, j3 = (
        Job(url=f"https://x.com/r{i}", title="Rust Dev", company="Acme", source="indeed")
        for i in range(3)
    )
    session.add_all([j1, j2, j3])
    await session.commit()
    await dedup.assign_dedup_groups()

    j1.is_active = False
    await session.commit()
    assert await dedup.assign_dedup_groups() == 1
    assert await _group_ids(session, j2, j3) == [j2.id, j2.id]


@pytest.mark.asyncio
async def test_title_change_leaves_the_group(patched_db, session):
    j1 = Job(url="https://x.com/t1", title="QA Engineer", company="Acme", source="indeed")
    j2 = Job(url="https://x.com/t2", title="QA Engineer", company="Acme", source="ejobs")
    session.add_all([j1, j2])
    await session.commit()
    await dedup.assign_dedup_groups()

    j2.title = "Senior QA Engineer"
    await session.commit()
    await dedup.assign_dedup_groups()
    assert await _group_ids(session, j1, j2) == [None, None]


@pytest.mark.asyncio
async def test_canonical_aging_out_of_the_window_releases_the_member(patched_db, session):
    """Feed and grouping use a 30-day window: a member whose canonical aged
    out of it would otherwise be hidden from a feed that no longer has the
    canonical either."""
    old = Job(url="https://x.com/a1", title="Data Analyst", company="Acme", source="indeed")
    old.first_seen_at = datetime.now(timezone.utc) - timedelta(days=40)
    recent = Job(url="https://x.com/a2", title="Data Analyst", company="Acme", source="ejobs")
    session.add_all([old, recent])
    await session.commit()
    old.dedup_group_id = old.id
    recent.dedup_group_id = old.id  # grouped back when both were recent
    await session.commit()

    await dedup.assign_dedup_groups()
    assert await _group_ids(session, recent) == [None]


@pytest.mark.asyncio
async def test_older_rows_follow_their_canonical(patched_db, session):
    """Outside the window rows are not regrouped, but a duplicate is only
    hidden while its canonical is still live."""
    long_ago = datetime.now(timezone.utc) - timedelta(days=60)
    rows = [
        Job(url=f"https://x.com/o{i}", title=title, company="Acme", source="indeed")
        for i, title in enumerate(("Java Dev", "Java Dev", "SRE", "SRE"))
    ]
    for job in rows:
        job.first_seen_at = long_ago
    session.add_all(rows)
    await session.commit()
    live_canon, kept, dead_canon, orphan = rows
    live_canon.dedup_group_id = live_canon.id
    kept.dedup_group_id = live_canon.id
    dead_canon.dedup_group_id = dead_canon.id
    dead_canon.is_active = False
    orphan.dedup_group_id = dead_canon.id
    await session.commit()

    await dedup.assign_dedup_groups()
    assert await _group_ids(session, kept, orphan) == [live_canon.id, None]


def test_normalize_key_folds_diacritics():
    """The separators are ASCII: "București" split into "bucure" + "ti",
    and "Română" lost its "a" — so the same posting on two boards, one
    spelling with diacritics and one without, never grouped."""
    assert dedup.normalize_key(
        "Programator Junior – București", "Societatea Română SRL",
    ) == dedup.normalize_key("Programator Junior Bucuresti", "Societatea Romana S.R.L.")


def test_normalize_key_reduces_local_entities_to_the_brand():
    """eJobs names the Romanian entity, LinkedIn the brand."""
    brand = dedup.normalize_key("Java Developer", "Endava")
    assert dedup.normalize_key("Java Developer", "ENDAVA ROMANIA SRL") == brand
    assert dedup.normalize_key("Java Developer", "S.C. Acme S.R.L.") == dedup.normalize_key(
        "Java Developer", "Acme",
    )
    # A name that IS the country is not emptied.
    assert dedup.normalize_key("Java Developer", "Romania") is not None


def test_locations_compatible():
    assert not dedup.locations_compatible("Cluj-Napoca, Romania", "Iași, Romania")
    assert dedup.locations_compatible("Cluj-Napoca, CJ, Romania", "Cluj-Napoca, Cluj, Romania")
    assert dedup.locations_compatible("București, IF, RO", "Bucharest, Romania")
    # No city on one side, or a multi-city listing that includes it.
    assert dedup.locations_compatible("Cluj-Napoca, Romania", "Romania")
    assert dedup.locations_compatible("Remote", "Iasi, Romania")
    assert dedup.locations_compatible("Baia Mare, Cluj-Napoca, Romania", "Cluj-Napoca, Romania")


@pytest.mark.asyncio
async def test_same_role_in_two_cities_is_two_postings(patched_db, session):
    """Hiding the Iasi opening behind the Cluj one made it unfindable from an
    Iasi search: browse filters by location after dropping duplicates."""
    cluj_li = Job(
        url="https://linkedin.com/c", title="Java Developer", company="Endava",
        location="Cluj-Napoca, Cluj, Romania", source="linkedin",
    )
    iasi_li = Job(
        url="https://linkedin.com/i", title="Java Developer", company="Endava",
        location="Iasi, Iasi, Romania", source="linkedin",
    )
    cluj_ej = Job(
        url="https://ejobs.ro/c", title="Java Developer", company="ENDAVA ROMANIA SRL",
        location="Cluj-Napoca, Romania", source="ejobs",
    )
    session.add_all([cluj_li, iasi_li, cluj_ej])
    await session.commit()

    assert await dedup.assign_dedup_groups() == 1
    assert await _group_ids(session, cluj_li, iasi_li, cluj_ej) == [
        cluj_li.id, None, cluj_li.id,
    ]

    from gosha.services import jobs as jobs_service

    page, total = await jobs_service.list_jobs(
        user_id=1, filters=jobs_service.JobFilters(locations=["iasi"]), page=1, per_page=50,
    )
    assert [job.url for job in page] == ["https://linkedin.com/i"]


def test_shared_city_or_no_city_still_groups():
    """A multi-city listing shares a city with each single-city copy, and a
    remote copy names no city at all."""
    assert dedup.locations_compatible("Cluj-Napoca, Bucuresti, Iasi", "Iasi")
    assert dedup.locations_compatible("Remote", "Cluj-Napoca, Romania")


@pytest.mark.asyncio
async def test_multi_city_and_remote_copies_group(patched_db, session):
    multi = Job(
        url="https://ejobs.ro/m", title="QA Engineer", company="Acme",
        location="Cluj-Napoca, Bucuresti, Iasi", source="ejobs",
    )
    iasi = Job(
        url="https://linkedin.com/m", title="QA Engineer", company="Acme",
        location="Iasi", source="linkedin",
    )
    remote = Job(
        url="https://x.com/r", title="Data Engineer", company="Acme",
        location="Remote", source="bestjobs",
    )
    cluj = Job(
        url="https://y.com/r", title="Data Engineer", company="Acme",
        location="Cluj", source="hipo",
    )
    session.add_all([multi, iasi, remote, cluj])
    await session.commit()

    assert await dedup.assign_dedup_groups() == 2
    assert await _group_ids(session, multi, iasi, remote, cluj) == [
        multi.id, multi.id, remote.id, remote.id,
    ]


# ── The same posting DM'd twice (prod, 2026-10-12..15) ──────────────────
# 10 of the 130 DMs sent were a posting the user already had from another
# board, under another spelling of the employer.


@pytest.mark.parametrize(
    ("title", "a", "b"),
    [
        ("Internship - Local IT - Lugoj", "FORVIA HELLA", "HELLA"),
        ("IT SACM Coordinator - Internship (1-Year Fixed-Term Contract, with Possibility of Extension)",
         "DRÄXLMAIER Group", "DRÄXLMAIER"),
        ("ExStream Developer Internship",
         "Societe Generale Global Solution Centre Romania", "Société Générale"),
        ("Network Engineer with German - Junior Consultant", "MHP – A Porsche Company", "MHP"),
        ("Java Developer", "Endava Holding", "Endava"),
    ],
)
def test_normalize_key_reduces_groups_parents_and_local_centres(title, a, b):
    assert dedup.normalize_key(title, a) == dedup.normalize_key(title, b)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("Banca Transilvania", "Transilvania Bank"),
        ("Bosch", "Boschung"),
        ("IT Services", "Group"),
    ],
)
def test_normalize_key_keeps_different_employers_apart(a, b):
    assert dedup.normalize_key("Java Developer", a) != dedup.normalize_key("Java Developer", b)


def test_normalize_key_keeps_a_name_made_only_of_noise_words():
    # Nothing but "group"/"services" left to strip: the name itself is kept.
    assert dedup.normalize_key("Java Developer", "Global Services Group") is not None


async def test_an_employers_longer_local_name_groups_with_the_brand(patched_db, session):
    """Undelucram.ro reposts for "Continental Anvelope Romania" (the name
    gosha.scraper takes from "on behalf of:"); LinkedIn has Continental's own
    copy. Same title, same city."""
    own = Job(url="https://linkedin.com/jobs/view/7622", title="Internship IT (8h) - Tires",
              company="Continental", location="Timişoara, Timiş, Romania", source="linkedin")
    repost = Job(url="https://linkedin.com/jobs/view/7621", title="Internship IT (8h) - Tires",
                 company="Continental Anvelope Romania", location="Timisoara Metropolitan Area",
                 source="linkedin")
    other_city = Job(url="https://linkedin.com/jobs/view/9", title="Internship IT (8h) - Tires",
                     company="Continental", location="Sibiu, Romania", source="linkedin")
    session.add_all([own, repost, other_city])
    await session.commit()
    assert await dedup.assign_dedup_groups() == 1
    assert await _group_ids(session, own, repost, other_city) == [own.id, own.id, None]


async def test_reposter_joins_the_employer_its_posting_names(patched_db, session):
    """Hipo.ro reposts AUMOVIO's internship on LinkedIn under its own name;
    the description names AUMOVIO. A reposter posting that names nobody else
    in the group stays on its own."""
    own = Job(url="https://linkedin.com/jobs/view/7921",
              title="Hardware Test Engineer – Internship (Iasi)", company="AUMOVIO",
              location="Iași, Braşov, Romania", source="linkedin")
    repost = Job(url="https://linkedin.com/jobs/view/8011",
                 title="Hardware Test Engineer - Internship (Iasi)", company="Hipo.ro",
                 location="Ilfov, Romania", source="linkedin",
                 description="**Company Description** Since its spin-off in September 2025 "
                             "AUMOVIO continues the business of the former Continental Automotive.")
    unnamed = Job(url="https://linkedin.com/jobs/view/7437",
                  title="Hardware Test Engineer - Internship (Iasi)", company="Hipo.ro",
                  location="Iasi, Romania", source="linkedin",
                  description="Peste 100 de ani de incredere si traditie.")
    session.add_all([own, repost, unnamed])
    await session.commit()
    assert await dedup.assign_dedup_groups() == 1
    assert await _group_ids(session, own, repost, unnamed) == [own.id, own.id, None]


async def test_two_brands_sharing_a_word_are_not_merged(patched_db, session):
    a = Job(url="https://x/a", title="Data Analyst", company="Banca Transilvania",
            location="Cluj-Napoca, Romania", source="ejobs")
    b = Job(url="https://x/b", title="Data Analyst", company="Transilvania Bank",
            location="Cluj-Napoca, Romania", source="linkedin")
    session.add_all([a, b])
    await session.commit()
    assert await dedup.assign_dedup_groups() == 0


def test_same_posting_matches_what_grouping_matches():
    """For gosha.pipeline's check of a user's expired deliveries, which
    compared normalize_key only."""
    t = "Internship IT (8h) - Tires"
    assert dedup.same_posting(t, "Continental Anvelope Romania", t, "Continental")
    assert dedup.same_posting("Internship - Local IT - Lugoj", "FORVIA HELLA",
                              "Internship - Local IT - Lugoj", "HELLA")
    assert not dedup.same_posting(t, "Continental", "Data Analyst", "Continental")
    assert not dedup.same_posting("Data Analyst", "Banca Transilvania", "Data Analyst", "Transilvania Bank")
    assert not dedup.same_posting("Data Analyst", "Unknown", "Data Analyst", "Unknown")
    # A reposter with its text: matched when the text names the employer.
    assert dedup.same_posting("Hardware Test Engineer", "Hipo.ro", "Hardware Test Engineer", "AUMOVIO",
                              text_a="Since its spin-off in September 2025 AUMOVIO continues")
    assert not dedup.same_posting("Hardware Test Engineer", "Hipo.ro", "Hardware Test Engineer", "AUMOVIO")


_MARSH_TITLE = "Junior Full-Stack Engineer, Generative AI @ Marsh Tech"


def test_an_employer_named_in_the_title_is_the_employer():
    """Prod 7210/8041 (2026-10-16): LinkedIn says Marsh, BestJobs names Marsh
    McLennan's entity "MMC Corporate"; the title names the unit. One user got
    both DMs."""
    assert dedup.normalize_key(_MARSH_TITLE, "Marsh") == dedup.normalize_key(
        _MARSH_TITLE, "MMC Corporate",
    )
    assert dedup.same_posting(_MARSH_TITLE, "Marsh", _MARSH_TITLE, "MMC Corporate")
    t = "Junior Business Data Analyst @MarshTech"
    assert dedup.same_posting(t, "Marsh", t, "Mercer")
    # Without the "@ unit" the employers still decide.
    assert not dedup.same_posting("Data Analyst", "Marsh", "Data Analyst", "MMC Corporate")
    # The unit is part of the title: another unit's posting stays apart.
    assert not dedup.same_posting(_MARSH_TITLE, "Marsh", _MARSH_TITLE.replace(
        "Marsh Tech", "DARWIN"), "Marsh")


async def test_copies_whose_title_names_the_employer_group(patched_db, session):
    linkedin = Job(url="https://linkedin.com/jobs/view/7210", title=_MARSH_TITLE, company="Marsh",
                   location="Cluj-Napoca, Cluj, Romania", source="linkedin")
    bestjobs = Job(url="https://www.bestjobs.eu/ro/loc-de-munca/x", title=_MARSH_TITLE,
                   company="MMC Corporate", location="Cluj-Napoca, România", source="bestjobs")
    session.add_all([linkedin, bestjobs])
    await session.commit()
    assert await dedup.assign_dedup_groups() == 1
    assert await _group_ids(session, linkedin, bestjobs) == [linkedin.id, linkedin.id]
