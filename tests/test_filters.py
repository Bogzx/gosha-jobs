"""Tests for the filtering and relevance logic."""

from __future__ import annotations

import pandas as pd

from gosha.filters import (
    city_only,
    expand_keyword,
    filter_dataframe,
    fold,
    is_romanian_location,
    keyword_wants_entry_level,
    keyword_wants_tech,
    location_matches,
    matches_company_blacklist,
    matches_excluded_keywords,
    matches_experience_level,
    normalize_location,
    title_is_relevant,
)

# ── normalize_location ────────────────────────────────────────────────


class TestNormalizeLocation:
    def test_known_alias(self):
        search, subs = normalize_location("Cluj")
        assert search == "Cluj-Napoca, Romania"
        assert "cluj" in subs

    def test_case_insensitive(self):
        search, subs = normalize_location("BUCHAREST")
        assert search == "Bucharest, Romania"
        assert "bucharest" in subs

    def test_unknown_location_passthrough(self):
        search, subs = normalize_location("New York")
        assert search == "New York"
        assert subs == ["new york"]

    def test_romania_expands_to_multiple_cities(self):
        search, subs = normalize_location("romania")
        assert search == "Romania"
        assert "bucharest" in subs
        assert "cluj" in subs


# ── expand_keyword ────────────────────────────────────────────────────


class TestExpandKeyword:
    def test_known_keyword_expands(self):
        terms = expand_keyword("computer science internship")
        assert len(terms) > 10
        assert "software engineer intern" in terms

    def test_unknown_keyword_passthrough(self):
        terms = expand_keyword("underwater basket weaving")
        assert terms == ["underwater basket weaving"]

    def test_case_insensitive(self):
        terms = expand_keyword("Data Science")
        assert "data scientist" in terms


# ── location_matches ──────────────────────────────────────────────────


class TestLocationMatches:
    def test_match(self):
        assert location_matches("Cluj-Napoca, Romania", ["cluj"]) is True

    def test_no_match(self):
        assert location_matches("Berlin, Germany", ["cluj"]) is False

    def test_empty_location_excludes(self):
        assert location_matches("", ["cluj"]) is False

    def test_nan_location_excludes(self):
        assert location_matches("nan", ["cluj"]) is False

    def test_none_location_excludes(self):
        assert location_matches(None, ["cluj"]) is False  # type: ignore[arg-type]

    def test_multiple_substrings(self):
        assert location_matches("București, Romania", ["bucuresti", "bucurești"]) is True


# ── keyword_wants_entry_level ─────────────────────────────────────────


class TestKeywordWantsEntryLevel:
    def test_internship(self):
        assert keyword_wants_entry_level("computer science internship") is True

    def test_junior(self):
        assert keyword_wants_entry_level("junior developer") is True

    def test_generic(self):
        assert keyword_wants_entry_level("software engineer") is False


# ── keyword_wants_tech ────────────────────────────────────────────────


class TestKeywordWantsTech:
    def test_known_expansion(self):
        assert keyword_wants_tech("computer science") is True

    def test_tech_keyword(self):
        assert keyword_wants_tech("cloud engineer") is True

    def test_non_tech(self):
        assert keyword_wants_tech("marketing manager") is False


# ── title_is_relevant ─────────────────────────────────────────────────


class TestTitleIsRelevant:
    def test_intern_title_for_intern_keyword(self):
        assert title_is_relevant("Software Engineer Intern", "computer science internship") is True

    def test_senior_title_rejected_for_intern_keyword(self):
        assert title_is_relevant("Senior Software Engineer", "computer science internship") is False

    def test_non_entry_rejected_for_intern_keyword(self):
        assert title_is_relevant("Software Engineer", "computer science internship") is False

    def test_tech_title_for_tech_keyword(self):
        assert title_is_relevant("Backend Developer", "software engineering") is True

    def test_non_tech_rejected(self):
        assert title_is_relevant("Marketing Manager", "software engineering") is False

    def test_generic_keyword_passes_all(self):
        assert title_is_relevant("Anything Goes", "random keyword") is True

    def test_empty_title_rejected(self):
        assert title_is_relevant("", "software engineering") is False

    # Real leaks observed in production deliveries (2026-06-10): generic
    # words like "engineer"/"technology"/"AI" let non-CS roles through.

    def test_industrial_engineering_rejected_for_tech(self):
        assert title_is_relevant(
            "Junior Process Technology Engineer - Zalau", "computer science internship"
        ) is False
        assert title_is_relevant(
            "Junior Maintenance Engineer (Mech. or Electrical)", "computer science internship"
        ) is False

    def test_marketing_with_ai_buzzword_rejected_for_tech(self):
        assert title_is_relevant(
            "Junior Marketing & Proiecte antreprenoriale si AI", "computer science internship"
        ) is False

    def test_strong_cs_signal_overrides_negative_domain(self):
        # "process" appears, but this is unambiguously a software role
        assert title_is_relevant(
            "Junior Software Engineer - Process Automation", "computer science internship"
        ) is True
        # Marketing-tech crossover with an explicit developer signal stays
        assert title_is_relevant(
            "Junior Web Developer (Marketing Team)", "computer science internship"
        ) is True


# ── matches_excluded_keywords ─────────────────────────────────────────


class TestMatchesExcludedKeywords:
    def test_match(self):
        assert matches_excluded_keywords("Sales Engineer at TechCo", ["sales"]) is True

    def test_no_match(self):
        assert matches_excluded_keywords("Backend Developer", ["sales"]) is False

    def test_empty_exclusions(self):
        assert matches_excluded_keywords("Anything", []) is False

    def test_case_insensitive(self):
        assert matches_excluded_keywords("SALES rep", ["sales"]) is True


# ── matches_company_blacklist ─────────────────────────────────────────


class TestMatchesCompanyBlacklist:
    def test_match(self):
        assert matches_company_blacklist("Accenture", ["Accenture", "Deloitte"]) is True

    def test_no_match(self):
        assert matches_company_blacklist("Google", ["Accenture"]) is False

    def test_case_insensitive(self):
        assert matches_company_blacklist("accenture", ["Accenture"]) is True

    def test_empty_blacklist(self):
        assert matches_company_blacklist("Anyone", []) is False


# ── matches_experience_level ──────────────────────────────────────────


class TestMatchesExperienceLevel:
    def test_any_matches_everything(self):
        assert matches_experience_level("CEO", ["any"]) is True

    def test_empty_matches_everything(self):
        assert matches_experience_level("CEO", []) is True

    def test_intern(self):
        assert matches_experience_level("Software Engineer Intern", ["intern"]) is True

    def test_junior(self):
        assert matches_experience_level("Junior Developer", ["junior"]) is True

    def test_mid_level(self):
        assert matches_experience_level("Software Engineer", ["mid"]) is True

    def test_mid_rejects_senior(self):
        assert matches_experience_level("Senior Engineer", ["mid"]) is False

    def test_mid_rejects_intern(self):
        assert matches_experience_level("Intern", ["mid"]) is False

    def test_senior(self):
        assert matches_experience_level("Senior Backend Developer", ["senior"]) is True

    def test_multiple_levels(self):
        assert matches_experience_level("Junior Dev", ["intern", "junior"]) is True
        assert matches_experience_level("Senior Dev", ["intern", "junior"]) is False

    # Production complaint (2026-06-10): intern-only searches received
    # junior roles. "intern" must mean internships/traineeships ONLY.

    def test_intern_rejects_junior_titles(self):
        assert matches_experience_level("Junior Developer", ["intern"]) is False
        assert matches_experience_level("Junior Software Engineer", ["intern"]) is False
        assert matches_experience_level("Graduate Software Engineer", ["intern"]) is False
        assert matches_experience_level("Entry Level Developer", ["intern"]) is False

    def test_intern_accepts_trainee_variants(self):
        assert matches_experience_level("Software Trainee", ["intern"]) is True
        assert matches_experience_level("Engineering Internship 2026", ["intern"]) is True
        assert matches_experience_level("Summer Intern - Backend", ["intern"]) is True
        # Romanian boards use these
        assert matches_experience_level("Practicant IT", ["intern"]) is True
        assert matches_experience_level("Stagiar dezvoltare software", ["intern"]) is True

    def test_junior_still_rejects_intern_only_titles(self):
        assert matches_experience_level("Software Engineer Intern", ["junior"]) is False


# ── city_only ─────────────────────────────────────────────────────────


class TestCityOnly:
    def test_city_country(self):
        assert city_only("Cluj-Napoca, Romania") == "Cluj-Napoca"

    def test_city_only_input(self):
        assert city_only("Dublin") == "Dublin"


# ── filter_dataframe ──────────────────────────────────────────────────


class TestFilterDataframe:
    def test_filters_irrelevant_titles(self):
        df = pd.DataFrame({
            "title": [
                "Software Engineer Intern",
                "Marketing Manager",
                "Data Analyst Intern",
                "Senior VP of Sales",
            ]
        })
        result = filter_dataframe(df, "computer science internship")
        assert len(result) == 2
        assert "Software Engineer Intern" in result["title"].values
        assert "Data Analyst Intern" in result["title"].values

    def test_empty_dataframe(self):
        df = pd.DataFrame()
        result = filter_dataframe(df, "anything")
        assert result.empty

    def test_no_title_column(self):
        df = pd.DataFrame({"company": ["Acme"]})
        result = filter_dataframe(df, "anything")
        assert len(result) == 1  # returned as-is

    def test_board_entry_level_keeps_a_plain_title(self):
        """Romanian boards file "QA Engineer" as entry-level without saying
        so in the title; the board's label is honoured."""
        df = pd.DataFrame({
            "title": ["QA Engineer", "QA Engineer", "Senior QA Engineer", "Java Developer"],
            "experience_level": ["entry", "mid", "entry", None],
        })
        result = filter_dataframe(df, "computer science internship")
        assert list(result["title"]) == ["QA Engineer"]
        assert list(result["experience_level"]) == ["entry"]


# ── word boundaries and Romanian vocabulary ───────────────────────────


class TestWordBoundaries:
    def test_international_and_internal_are_not_internships(self):
        for title in ("International Sales Manager", "Internal Auditor"):
            assert matches_experience_level(title, ["intern"]) is False
        assert title_is_relevant(
            "Internal Tools Engineer", "computer science internship",
        ) is False

    def test_plural_and_suffixed_forms_still_match(self):
        assert matches_experience_level("Summer Internships 2027", ["intern"]) is True
        assert matches_experience_level(
            "Year in Industry Placements - Technology", ["intern"],
        ) is True
        assert title_is_relevant(
            "Year in Industry Placements - Technology", "computer science internship",
        ) is True
        assert matches_experience_level("Engineering Apprenticeship", ["intern"]) is True
        assert title_is_relevant("Software Developers (Graduates)", "cs entry level") is True

    def test_romanian_entry_level_terms(self):
        for title in (
            "Programator Java Stagiar", "Practică de vară IT", "Absolvent IT - Tester",
            "Dezvoltator software fără experiență", "Programator începător",
        ):
            assert title_is_relevant(title, "computer science internship"), title

    def test_tech_terms_that_used_to_miss(self):
        for title in (
            "IT Support Intern", "Information Technology Intern", "Computing Intern",
            "Programator Junior", "Dezvoltator Junior", "Tester Junior",
            "Junior Software Developers",
        ):
            assert title_is_relevant(title, "computer science internship"), title

    def test_it_is_case_sensitive(self):
        assert title_is_relevant("Make it happen - Junior", "computer science internship") is False

    def test_romanian_trades_need_a_strong_cs_signal(self):
        for title in (
            "Tehnician Infrastructura IT & CCTV", "Inginer Mentenanta",
            "Inginer Calitate & BIM (Revit)", "Data Center Technician",
        ):
            assert title_is_relevant(title, "software engineering") is False, title
        assert title_is_relevant("Tehnician IT - Python developer", "software engineering")


# ── location folding ──────────────────────────────────────────────────


class TestLocationFolding:
    def test_fold_strips_both_romanian_comma_styles(self):
        assert fold("Timişoara") == fold("Timișoara") == "timisoara"
        assert fold("România") == "romania"

    def test_romania_matches_board_spellings(self):
        _search, subs = normalize_location("romania")
        assert location_matches("Cluj-Napoca, România", subs)
        assert location_matches("București, IF, RO", subs)  # Indeed's format
        assert not location_matches("Berlin, Germany", subs)

    def test_diacritic_aliases_resolve(self):
        assert normalize_location("Timișoara")[0] == "Timisoara, Romania"
        assert normalize_location("Iași")[0] == "Iasi, Romania"

    def test_is_romanian_location(self):
        for loc in ("cluj", "Bucharest", "romania", "europe", "Arad", "Cluj-Napoca"):
            assert is_romanian_location(loc), loc
        for loc in ("remote", "london", "berlin"):
            assert not is_romanian_location(loc), loc
