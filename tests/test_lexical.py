"""Tests for gosha/lexical.py: terms, the TF-IDF index and its caches."""

from __future__ import annotations

import numpy as np
import pytest

from gosha import lexical


def test_terms_fold_romanian_diacritics_in_both_forms():
    # comma-below (ț), cedilla (ţ) and no diacritics at all: one term.
    assert lexical.terms("experiență") == lexical.terms("experienţă") == ["experienta"]
    assert lexical.terms("Experienta") == ["experienta"]


def test_terms_keep_skill_names_whole():
    assert lexical.terms("C++, C#, Node.js and .NET.") == ["c++", "c#", "node.js", "and", "net"]
    # One-letter tokens and digits carry nothing.
    assert lexical.terms("a 3 R") == []


def test_scores_rank_shared_rare_terms_first():
    docs = ["Kotlin Android developer", "Java developer", "Accountant, Excel"]
    scores = lexical.scores("Kotlin developer", docs)
    assert np.argmax(scores) == 0
    assert scores[2] == 0


def test_counts_are_cached_per_key_and_evicted_least_recent_first(monkeypatch):
    monkeypatch.setattr(lexical, "MAX_CACHED", 2)
    lexical.LexicalIndex(["one", "two"], keys=["a", "b"])
    # "a" is read again, so "b" is the least recently used.
    lexical.LexicalIndex([None, "three"], keys=["a", "c"])
    assert lexical.is_cached("a") and lexical.is_cached("c")
    assert not lexical.is_cached("b")


def test_a_key_neither_given_nor_cached_is_a_miss_not_an_empty_posting():
    index = lexical.LexicalIndex(["kotlin", None], keys=["a", "gone"])
    assert not index.complete
    assert not lexical.is_cached("gone")
    assert list(index.score_keys("kotlin", ["a", "gone"])) == pytest.approx([1.0, 0.0])
    # Nothing built from the gap is reused for the next request.
    assert lexical.index_for(["kotlin", None], ["a", "gone"]) is not index
    assert not lexical._indexes


def test_index_for_reuses_only_an_index_over_exactly_these_keys():
    window = lexical.index_for(["kotlin", "java", "excel"], ["a", "b", "c"])
    assert lexical.index_for([None, None, None], ["c", "b", "a"]) is window
    # A subset gets its own index (its own IDF), never a superset's.
    subset = lexical.index_for([None, None], ["a", "b"])
    assert subset is not window and subset.size == 2


def test_index_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(lexical, "MAX_INDEXES", 2)
    first = lexical.index_for(["x1"], ["k1"])
    lexical.index_for(["x2"], ["k2"])
    lexical.index_for(["x3"], ["k3"])
    assert len(lexical._indexes) == 2
    assert first not in lexical._indexes.values()


def test_one_key_per_document():
    with pytest.raises(ValueError):
        lexical.LexicalIndex(["a", "b"], keys=["only one"])


def test_a_query_of_terms_no_posting_has_scores_nothing():
    index = lexical.LexicalIndex(["kotlin", "java"])
    assert not index.score("haskell").any()
