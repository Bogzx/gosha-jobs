"""Tests for gosha/seniority.py: posting levels (read with the hard
filters' detectors), CV levels and the penalty between them."""

from __future__ import annotations

import pytest

from gosha import filters, seniority
from gosha.seniority import CV_MID, CV_SENIOR, CV_UNKNOWN, ENTRY, JUNIOR, MID, SENIOR, STUDENT, UNKNOWN


@pytest.mark.parametrize("title, level", [
    ("Senior Java Developer", SENIOR),
    ("Sr. Data Engineer", SENIOR),
    ("Tech Lead Backend", SENIOR),
    ("Contabil Șef", SENIOR),
    ("Şef de echipă", SENIOR),          # cedilla form
    ("Staff Backend Engineer", SENIOR),
    ("Junior Python Developer", ENTRY),
    ("Stagiu vară IT", ENTRY),
    ("Software Engineering Intern", ENTRY),
    ("Working Student Data", ENTRY),
    ("Junior IT Project Manager", ENTRY),  # "manager" is the role here
    ("Mid-Level QA Engineer", MID),
    ("Data Scientist II", MID),
    ("Expert contabil", MID),               # an occupation, not a grade
    ("Staff Accountant", UNKNOWN),          # not the engineering grade
    ("Lead Generation Specialist", UNKNOWN),
    ("International Sales Representative", UNKNOWN),  # not "intern"
    ("Internal Auditor", UNKNOWN),
])
def test_title_decides(title, level):
    assert seniority.posting_level(title) == level


@pytest.mark.parametrize("title, level", [
    ("InternSoftware Engineer", ENTRY),     # a scraper dropped the space
    ("JuniorData Analyst", ENTRY),
    ("SeniorBackend Developer", SENIOR),
])
def test_a_glued_level_word_reads_as_the_filters_read_it(title, level):
    assert seniority.posting_level(title) == level


def test_title_outranks_the_board_label_and_the_label_outranks_the_text():
    assert seniority.posting_level("Senior Dev", None, "entry") == SENIOR
    assert seniority.posting_level("QA Engineer", "5+ years of experience", "entry") == ENTRY


@pytest.mark.parametrize("description, level", [
    ("We need 3+ years of experience with Java.", MID),
    ("Minim 2 ani experiență în contabilitate.", ENTRY),  # 2 is still junior
    ("At least 5 years of professional experience.", SENIOR),
    ("A company with over 20 years on the market.", UNKNOWN),
    ("Open to recent graduates, no prior experience required.", ENTRY),
    (None, UNKNOWN),
    ("", UNKNOWN),
])
def test_description_when_title_and_label_say_nothing(description, level):
    assert seniority.posting_level("Backend Developer", description) == level


def test_years_thresholds_follow_the_filters_junior_limit():
    # The DM level filter takes up to JUNIOR_MAX_YEARS as junior; the feed
    # must not call the same posting mid.
    text = f"Minimum {filters.JUNIOR_MAX_YEARS} years of experience required."
    assert filters.required_years(text) == filters.JUNIOR_MAX_YEARS
    assert seniority.posting_level("Analyst", text) == ENTRY


@pytest.mark.parametrize("cv, level", [
    ("Computer Science student, expected graduation 2027. Python, SQL.", STUDENT),
    ("Software developer. Python, Django.", JUNIOR),
    ("Backend engineer with 4 years of experience in Go and Kubernetes.", CV_MID),
    ("Senior Backend Engineer, 8 years of experience building payment systems.", CV_SENIOR),
    ("Lead Software Engineer with 5 years of experience.", CV_SENIOR),
    ("", CV_UNKNOWN),
    (None, CV_UNKNOWN),
])
def test_cv_level(cv, level):
    assert seniority.cv_level(cv) == level


def test_penalty_direction():
    # A student: entry lifted, senior pushed down hardest.
    assert seniority.level_penalty(STUDENT, ENTRY) < 0
    assert seniority.level_penalty(STUDENT, SENIOR) > seniority.level_penalty(STUDENT, MID) > 0
    # A senior is not shown internships first.
    assert seniority.level_penalty(CV_SENIOR, ENTRY) > seniority.level_penalty(CV_SENIOR, SENIOR)
    # Unknown on either side: no opinion.
    for reader in (STUDENT, JUNIOR, CV_MID, CV_SENIOR, CV_UNKNOWN):
        assert seniority.level_penalty(reader, UNKNOWN) == 0.0
    for posting in (ENTRY, MID, SENIOR, UNKNOWN):
        assert seniority.level_penalty(CV_UNKNOWN, posting) == 0.0
