"""Seniority: how senior a posting is, how senior a CV is, and the gap.

The offline eval (docs/EVAL.md) found that the embeddings cannot see
seniority at all: for the Python student the top results were "Senior
Python AI Engineer" and "Staff Backend Engineer", and the three Python
roles a student could apply to ranked 7th, 23rd and 31st. Telling the
ranking how far a posting is from the reader's level was the largest
single gain measured (feed nDCG@10 0.546 -> 0.67 on its own).

Pure functions, no IO: the feed (gosha/recommend.py) and the eval
(gosha/evaluation.py) call the same code.

A posting's level is read with the hard filters' own detectors
(gosha/filters.py: the title regexes, says_senior, required_years), so
the feed and the Discord level filter never disagree about whether a
posting is junior. In order of how much each signal can be trusted:
1. the title ("Senior", "Junior", "Intern", "Mid-level", ...);
2. the board's own label (Job.experience_level: intern/entry/mid/senior);
3. the years of experience the description requires ("3+ years of
   experience", "minim 2 ani experiență");
4. entry-level words in the description ("absolvent", "internship").
"""

from __future__ import annotations

import re

from gosha.filters import (
    ENTRY_LEVEL_RE,
    INTERN_RE,
    JUNIOR_MAX_YEARS,
    JUNIOR_RE,
    MID_TITLE_RE,
    required_years,
    says_senior,
    split_glued_level,
)

# Posting levels.
ENTRY, MID, SENIOR, UNKNOWN = "entry", "mid", "senior", "unknown"
# CV levels. CV_UNKNOWN: no CV text to read (a user ranked on 👍 alone, or
# a CV that cannot be decrypted), so no seniority adjustment at all.
STUDENT, JUNIOR, CV_MID, CV_SENIOR = "student", "junior", "mid", "senior"
CV_UNKNOWN = "unknown"

# Required years from this up read as senior; up to JUNIOR_MAX_YEARS (the
# junior level's own limit in gosha/filters.py) as entry; between, mid.
SENIOR_MIN_YEARS = 5
# Entry-level words in a description, for a posting whose title, label and
# requirements say nothing. Ranking only: the filters read titles.
_ENTRY_TEXT = re.compile(
    r"(?i)\b(?:internship|entry.?level|no (?:prior )?experience (?:is )?required|"
    r"recent graduates?|absolvent\w*|f[aă]r[aă]\s+experien[tț][aă]|stagiu|"
    r"program de (?:internship|practic[aă]))(?!\w)"
)
_BOARD_LEVEL = {"intern": ENTRY, "entry": ENTRY, "mid": MID, "senior": SENIOR}


def posting_level(
    title: str, description: str | None = None, board_level: str | None = None,
) -> str:
    """ENTRY, MID, SENIOR or UNKNOWN for one posting."""
    title = split_glued_level(title or "")
    if INTERN_RE.search(title) or JUNIOR_RE.search(title) or ENTRY_LEVEL_RE.search(title):
        return ENTRY
    if says_senior(title):
        return SENIOR
    if MID_TITLE_RE.search(title):
        return MID
    if board_level in _BOARD_LEVEL:
        return _BOARD_LEVEL[board_level]
    years = required_years(description or None)
    if years is not None:
        if years >= SENIOR_MIN_YEARS:
            return SENIOR
        if years > JUNIOR_MAX_YEARS:
            return MID
        return ENTRY
    if description and _ENTRY_TEXT.search(description[:4000]):
        return ENTRY
    return UNKNOWN


_CV_STUDENT = re.compile(
    r"(?i)\b(?:student\w*|undergraduate|bachelor'?s? (?:student|candidate)|"
    r"expected graduation|anul\s+(?:[1-4]|i{1,3}|iv)|year\s+[1-4]\b|[1-4](?:st|nd|rd|th)\s+year|"
    r"masterand\w*|licen[tț][aă] (?:[iî]n curs|2\d{3})|internship|stagiu|practic[aă] de var[aă])"
)
_CV_YEARS = re.compile(
    r"(?i)(\d{1,2})\s*\+?\s*(?:years?|ani)\s+(?:of\s+)?(?:professional\s+|industry\s+|work\s+)?"
    r"(?:experience|experien[tț][aă])"
)
_CV_SENIOR_TITLE = re.compile(
    r"(?i)\b(?:senior|lead|principal|staff|head of|engineering manager|architect)\s+"
    r"(?:\w+\s+){0,2}(?:engineer|developer|programmer|scientist|analyst|architect|consultant)"
)


def cv_level(cv_text: str | None) -> str:
    """STUDENT, JUNIOR, CV_MID or CV_SENIOR, read from the CV text, or
    CV_UNKNOWN when there is none.

    A CV that states no level reads as JUNIOR: GOSHA is built for students
    and early careers, and a junior reading only mildly prefers junior
    postings. No CV text at all is no evidence either way.
    """
    if not cv_text or not cv_text.strip():
        return CV_UNKNOWN
    years = [int(m.group(1)) for m in _CV_YEARS.finditer(cv_text) if int(m.group(1)) < 40]
    most = max(years, default=0)
    if most >= 6 or (_CV_SENIOR_TITLE.search(cv_text) and most >= 4):
        return CV_SENIOR
    if most >= 3:
        return CV_MID
    if _CV_STUDENT.search(cv_text):
        return STUDENT
    return JUNIOR


# How far a posting is from the reader, in standard deviations of the
# reader's own score distribution (gosha/recommend.py ranks on z-scores).
# Positive pushes down, negative lifts. A student is not shown "Senior"
# before an internship that fits as well; a senior engineer is not shown
# internships. UNKNOWN is never penalised: most Romanian postings carry
# no level at all.
_PENALTY: dict[str, dict[str, float]] = {
    STUDENT: {ENTRY: -0.15, MID: 0.25, SENIOR: 1.0, UNKNOWN: 0.0},
    JUNIOR: {ENTRY: -0.1, MID: 0.15, SENIOR: 0.8, UNKNOWN: 0.0},
    CV_MID: {ENTRY: 0.4, MID: 0.0, SENIOR: 0.2, UNKNOWN: 0.0},
    CV_SENIOR: {ENTRY: 0.8, MID: 0.2, SENIOR: -0.1, UNKNOWN: 0.0},
}


def level_penalty(reader: str, posting: str) -> float:
    """0 for a CV_UNKNOWN (or unrecognised) reader."""
    return _PENALTY.get(reader, {}).get(posting, 0.0)
