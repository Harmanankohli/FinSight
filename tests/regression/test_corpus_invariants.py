"""Corpus-level invariants: structural properties every generated deck must satisfy.

Parametrized over fixture files in tests/regression/corpus/ × 3 output formats.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared.reports.extraction import _extract_deck_data, _TICKER_RE, fit_text

CORPUS_DIR = Path(__file__).parent / "corpus"
FIXTURE_NAMES = sorted(p.stem for p in CORPUS_DIR.glob("*.json"))


def _load_corpus(name: str) -> dict:
    return json.loads((CORPUS_DIR / f"{name}.json").read_text())


# ── Deck invariants (run for every corpus fixture) ─────────────────────────

@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_deck_generation_succeeds(fixture_name: str):
    """Generation never raises for any corpus fixture."""
    brief = _load_corpus(fixture_name)
    deck = _extract_deck_data(brief, "NVDA", "BUY", 0.85, "2024-01-15")
    assert deck is not None


@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_deck_no_markdown_debris(fixture_name: str):
    """No slide text contains #, |, or unprocessed newline debris."""
    brief = _load_corpus(fixture_name)
    deck = _extract_deck_data(brief, "NVDA", "BUY", 0.85, "2024-01-15")
    text_fields = [
        deck.executive_summary,
        *deck.risks,
        *deck.opportunities,
        *(s.body for s in deck.sections),
    ]
    for text in text_fields:
        assert "#" not in text, f"Markdown heading debris: {text[:50]}"
        assert "|" not in text, f"Table debris: {text[:50]}"
        assert "\n" not in text, f"Newline debris: {text[:50]}"


@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_peer_names_match_ticker_re(fixture_name: str):
    """All peer names match _TICKER_RE."""
    brief = _load_corpus(fixture_name)
    deck = _extract_deck_data(brief, "NVDA", "BUY", 0.85, "2024-01-15")
    for name in deck.peer_names:
        assert _TICKER_RE.match(name), f"Invalid peer name: {name}"


@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_text_fits_estimator(fixture_name: str):
    """Executive summary should fit within estimate bounds (fit_text oracle)."""
    brief = _load_corpus(fixture_name)
    deck = _extract_deck_data(brief, "NVDA", "BUY", 0.85, "2024-01-15")
    if not deck.executive_summary:
        return
    # Simulate thesis slide box: 10.8in wide × 3.6in tall
    fitted, size = fit_text(deck.executive_summary, 10.8, 3.6)
    assert len(fitted) >= len(deck.executive_summary) * 0.5, "fit_text over-truncated"


@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_valuation_dollar_figures_unique(fixture_name: str):
    """Each dollar figure appears at most once in valuation_table (scenarios
    intentionally overlap — renderer deduplicates per P5)."""
    brief = _load_corpus(fixture_name)
    deck = _extract_deck_data(brief, "NVDA", "BUY", 0.85, "2024-01-15")
    dollar_values = [v for _, v in deck.valuation_table if v.startswith("$")]
    assert len(dollar_values) == len(set(dollar_values)), (
        "Duplicate dollar values in valuation_table: "
        f"{[v for v in dollar_values if dollar_values.count(v) > 1]}"
    )


@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_slide_count_reasonable(fixture_name: str):
    """Slide count in reasonable range [4, 14] based on extraction."""
    brief = _load_corpus(fixture_name)
    deck = _extract_deck_data(brief, "NVDA", "BUY", 0.85, "2024-01-15")
    n = sum([
        bool(deck.executive_summary),
        bool(deck.kpi_chips),
        bool(deck.financials),
        bool(deck.valuation_table or deck.scenarios),
        len(deck.scorecard) > 1,
        bool(deck.peer_names),
        bool(deck.opportunities or deck.risks),
        len(deck.sections),
    ])
    # title + conclusion are always present, + the data-driven slides
    total = 2 + n
    assert 4 <= total <= 14, f"Slide count {total} out of range [4, 14]"


# ── Risk/Reward item invariants ────────────────────────────────────────────

@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_risk_reward_items_no_debris(fixture_name: str):
    """No risk/reward item contains markdown or bare figures."""
    brief = _load_corpus(fixture_name)
    deck = _extract_deck_data(brief, "NVDA", "BUY", 0.85, "2024-01-15")
    for item in deck.risks + deck.opportunities:
        assert "#" not in item, f"MD debris in item: {item}"
        assert "|" not in item, f"Table debris in item: {item}"
        assert "\n" not in item, f"Newline in item: {item}"
        assert len(item) >= 6, f"Item too short: {item}"
        assert len(item) <= 200, f"Item too long ({len(item)} chars): {item[:50]}..."


@pytest.mark.parametrize("fixture_name", FIXTURE_NAMES)
def test_provenance_flags_invariants(fixture_name: str):
    """risks_extracted/opportunities_extracted match reality."""
    brief = _load_corpus(fixture_name)
    deck = _extract_deck_data(brief, "NVDA", "BUY", 0.85, "2024-01-15")
    if deck.risks_extracted:
        assert deck.risks, "risks_extracted=True but risks list empty"
    if deck.opportunities_extracted:
        assert deck.opportunities, "opportunities_extracted=True but opportunities list empty"
