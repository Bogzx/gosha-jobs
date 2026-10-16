"""Seniority: how senior a posting is, how senior a CV is, and the gap.

The offline eval (docs/EVAL.md) found that the embeddings cannot see
seniority at all: for the Python student the top results were "Senior
Python AI Engineer" and "Staff Backend Engineer", and the three Python
roles a student could apply to ranked 7th, 23rd and 31st. Telling the
ranking how far a posting is from the reader's level was the largest
single gain measured (feed nDCG@10 0.546 -> 0.67 on its own).

Pure functions, no IO: the feed (gosha/recommend.py) and the eval
(gosha/evaluation.py) call the same code.

Postings are read in order of how much each signal can be trusted:
1. the title ("Senior", "Junior", "Intern", "Lead", ...);
2. the board's own label (Job.experience_level: intern/entry/mid/senior);
3. the years of experience the description asks for ("3+ years of
   experience", "minim 2 ani experiență");
4. entry-level words in the description ("absolvent", "internship").
"""

from __future__ import annotations

import re

# Posting levels.
ENTRY, MID, SENIOR, UNKNOWN = "entry", "mid", "senior", "unknown"
# CV levels.
STUDENT, JUNIOR, CV_MID, CV_SENIOR = "student", "junior", "mid", "senior"

_SENIOR_TITLE = re.compile(
    r"(?i)\b(?:senior|sr\.?|lead|principal|staff|director|manager|head|vp|chief|"
    r"architect|expert|[sș]ef|coordonator|team\s*leader)\b"
)
_MID_TITLE = re.compile(r"(?i)\b(?:mid|medior|middle|intermediate|mid-level)\b")
# \b is Unicode-aware, so "practică" ends where it should. "Intern" must be
# a whole word: "International Sales" and "Internal Auditor" are not.
_ENTRY_TITLE = re.compile(
    r"(?i)\b(?:intern(?:ship)?s?|junior|jr\.?|graduates?|entry.?level|trainees?|"
    r"traineeship|apprentice(?:ship)?s?|stagiar\w*|stagi[ui]|practic(?:ant\w*|[aă])|"
    r"absolvent\w*|student\w*|[iî]ncep[aă]tor\w*|debutant\w*|"
    r"f[aă]r[aă]\s+experien[tț][aă])(?!\w)"
)
_ENTRY_TEXT = re.compile(
    r"(?i)\b(?:internship|entry.?level|no (?:prior )?experience (?:is )?required|"
    r"recent graduates?|absolvent\w*|f[aă]r[aă]\s+experien[tț][aă]|stagiu|"
    r"program de (?:internship|practic[aă]))(?!\w)"
)
# "3+ years", "2-4 years", "minim 2 ani". Only counted with an experience
# word nearby: "over 20 years on the market" is the company's age, not a
# requirement.
_YEARS = re.compile(
    r"(?i)(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to|la)\s*\d{1,2}\s*)?"
    r"(?:years?|yrs?|ani|de\s+ani)\b"
)
_EXPERIENCE_WORD = re.compile(r"(?i)experien|\bexp\b|vechime")
# A year count followed by this is about the company, not the candidate.
_COMPANY_AGE = re.compile(r"(?i)^\W*(?:of\s+)?(?:history|on the market|pe pia[tț][aă]|in business)")
_BOARD_LEVEL = {"intern": ENTRY, "entry": ENTRY, "mid": MID, "senior": SENIOR}


def required_years(text: str | None) -> int | None:
    """The years of experience a posting asks for, if it says.

    The first mention with an experience word within 60 characters: the
    first is almost always the headline requirement ("3+ years of Java"),
    later ones qualify it ("1 year with Kafka is a plus").
    """
    if not text:
        return None
    for m in _YEARS.finditer(text[:4000]):
        years = int(m.group(1))
        if not 0 < years < 16:
            continue
        window = text[max(0, m.start() - 60): m.end() + 60]
        if not _EXPERIENCE_WORD.search(window) or _COMPANY_AGE.match(text[m.end():]):
            continue
        return years
    return None


def posting_level(
    title: str, description: str | None = None, board_level: str | None = None,
) -> str:
    """ENTRY, MID, SENIOR or UNKNOWN for one posting."""
    title = title or ""
    if _ENTRY_TITLE.search(title):
        return ENTRY
    if _SENIOR_TITLE.search(title):
        return SENIOR
    if _MID_TITLE.search(title):
        return MID
    if board_level in _BOARD_LEVEL:
        return _BOARD_LEVEL[board_level]
    years = required_years(description)
    if years is not None:
        if years >= 5:
            return SENIOR
        if years >= 2:
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
    """STUDENT, JUNIOR, CV_MID or CV_SENIOR, read from the CV text.

    Unknown defaults to JUNIOR: GOSHA is built for students and early
    careers, and a junior reading only mildly prefers junior postings.
    """
    if not cv_text:
        return JUNIOR
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
    return _PENALTY.get(reader, _PENALTY[JUNIOR]).get(posting, 0.0)
