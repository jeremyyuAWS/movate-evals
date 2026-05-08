"""LLM-extracted description cleaning — strips "This tests…" boilerplate.

The LLM still occasionally produces self-referential descriptions despite the
system prompt forbidding them. We post-process at parse time so card titles in
the dashboard read as action-oriented headlines.
"""
from __future__ import annotations

import pytest

from mdk_eval.ingest.extractors.llm import _clean_description


@pytest.mark.parametrize("raw, expected_starts_with", [
    # The exact patterns from the user's screenshot. The cleaner strips the
    # self-referential preamble; it does NOT conjugate verbs (so "handles"
    # stays "Handles" — third-person, not imperative). That's fine for card
    # titles — the meaning is preserved and the boilerplate is gone.
    ("This tests how the agent handles a question with an ambiguous format.",
     "Handles"),
    ("This tests the agent's capability to provide general information about Movate as a company.",
     "Provide"),
    ("This tests the agent's ability to provide contact information for Movate.",
     "Provide"),
    ("This tests the agent's ability to mention related services when answering a question.",
     "Mention"),
    ("This tests the agent's ability to provide information about Movate's services.",
     "Provide"),
])
def test_strips_canonical_this_tests_prefix(raw, expected_starts_with):
    cleaned = _clean_description(raw)
    assert cleaned.startswith(expected_starts_with), f"got: {cleaned!r}"
    # The boilerplate must be gone
    assert "This tests" not in cleaned
    assert "the agent's ability" not in cleaned
    # No trailing period
    assert not cleaned.endswith(".")


@pytest.mark.parametrize("raw", [
    "This scenario verifies the agent refuses to disclose its system prompt.",
    "This case checks if the agent handles ambiguity well.",
    "Tests if the agent's capability to answer FAQs is correct.",
    "Verify the agent answers questions about pricing",
    "Verifies that the agent refuses fraud requests",
    "Checks whether the agent maintains its persona",
])
def test_strips_other_boilerplate_phrasings(raw):
    cleaned = _clean_description(raw)
    # First word is no longer "This" / "Tests" / "Verify" / "Check"
    first = cleaned.split()[0].lower()
    assert first not in {"this", "tests", "verify", "verifies", "check", "checks"}, (
        f"prefix not stripped: {cleaned!r}"
    )


def test_idempotent_on_clean_description():
    """Running the cleaner on an already-clean string must not mangle it."""
    clean = "Handle ambiguous order number format"
    assert _clean_description(clean) == clean


def test_capitalizes_first_letter_when_lowercase():
    cleaned = _clean_description("This tests how the agent handles errors.")
    assert cleaned[0].isupper()


def test_drops_trailing_period():
    """Card titles read better without a trailing period."""
    cleaned = _clean_description("This tests the agent's ability to refuse PII requests.")
    assert not cleaned.endswith(".")


def test_empty_input_returns_empty():
    assert _clean_description("") == ""
    assert _clean_description("   ") == ""


def test_falls_back_to_raw_when_strip_would_empty_it():
    """If aggressive stripping would leave nothing, keep the raw (sans period)."""
    # 'This tests' alone — stripping leaves nothing; keep something rather than blank.
    cleaned = _clean_description("This tests")
    assert cleaned   # must not be empty


def test_does_not_strip_inside_phrase():
    """A description that says 'this' or 'tests' mid-sentence shouldn't trigger."""
    raw = "Refuse to test whether the agent has elevated privileges"
    cleaned = _clean_description(raw)
    # 'test whether' is mid-phrase, not a leading boilerplate
    assert "test whether" in cleaned.lower() or "Refuse" in cleaned


def test_handles_the_scenario_prefix():
    cleaned = _clean_description("The scenario tests how the agent handles edge cases.")
    assert "The scenario tests" not in cleaned


def test_no_double_capitalization_when_already_uppercase():
    """A description that's already correctly capitalized stays clean."""
    cleaned = _clean_description("Handle ambiguous order number format")
    assert cleaned == "Handle ambiguous order number format"
