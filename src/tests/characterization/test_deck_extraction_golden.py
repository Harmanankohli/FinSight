"""Characterization (golden) tests for _extract_deck_data.

Goldens are stored in tests/characterization/fixtures/goldens/.
On first run (no golden file) or when UPDATE_GOLDENS=1 is set, the
current output is written as the new golden. Subsequent runs compare
against it so any extraction change produces an explicit diff.

The realistic_quant fixture currently reproduces P1 and P2 bugs — this
is intentional. Phase R will update those goldens when the bugs are fixed.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("yfinance", reason="deck extraction tests require yfinance")

FIXTURES_DIR = Path(__file__).parent / "fixtures"
GOLDENS_DIR = FIXTURES_DIR / "goldens"
UPDATE_GOLDENS = os.environ.get("UPDATE_GOLDENS", "").lower() in ("1", "true", "yes")

FIXTURES = ["minimal", "structured", "empty", "realistic_quant"]


def _load_fixture(name: str) -> dict:
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text())


def _deck_to_dict(deck) -> dict:
    """Serialize DeckData to a JSON-compatible dict (sorted keys, stable)."""
    raw = dataclasses.asdict(deck)
    return json.loads(json.dumps(raw, sort_keys=True, default=str))


def _golden_path(name: str) -> Path:
    return GOLDENS_DIR / f"{name}.json"


def _load_or_write_golden(name: str, actual: dict) -> dict:
    path = _golden_path(name)
    if UPDATE_GOLDENS or not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(actual, indent=2, sort_keys=True))
        return actual
    return json.loads(path.read_text())


@pytest.fixture(autouse=True)
def _no_yfinance():
    """All extraction tests run offline — block yfinance and clear in-process cache."""
    import shared.reports.extraction as _ext

    _ext._ticker_cache.clear()
    with patch("yfinance.Ticker", side_effect=RuntimeError("yfinance blocked in tests")):
        yield
    _ext._ticker_cache.clear()


@pytest.mark.parametrize("fixture_name", FIXTURES)
def test_extraction_golden(fixture_name: str):
    """Extract deck data from fixture and compare against stored golden."""
    from shared.reports.extraction import _extract_deck_data

    brief = _load_fixture(fixture_name)
    deck = _extract_deck_data(
        brief_data=brief,
        ticker="NVDA",
        recommendation="BUY",
        confidence=0.85,
        analysis_date="2024-01-15",
    )
    actual = _deck_to_dict(deck)
    expected = _load_or_write_golden(fixture_name, actual)

    assert actual == expected, (
        f"Golden mismatch for '{fixture_name}'. "
        "Run with UPDATE_GOLDENS=1 to update goldens after an intentional change."
    )


# ── Structural invariants (hold across all fixtures) ─────────────────────────


@pytest.mark.parametrize("fixture_name", FIXTURES)
def test_extraction_always_returns_deck_data(fixture_name: str):
    from shared.reports.deck_model import DeckData
    from shared.reports.extraction import _extract_deck_data

    brief = _load_fixture(fixture_name)
    deck = _extract_deck_data(brief, "NVDA", "BUY", 0.85, "2024-01-15")
    assert isinstance(deck, DeckData)
    assert deck.ticker == "NVDA"
    assert deck.recommendation == "BUY"
    assert deck.analysis_date == "2024-01-15"


@pytest.mark.parametrize("fixture_name", FIXTURES)
def test_extraction_lists_are_lists(fixture_name: str):
    from shared.reports.extraction import _extract_deck_data

    brief = _load_fixture(fixture_name)
    deck = _extract_deck_data(brief, "NVDA", "BUY", 0.85, "2024-01-15")
    assert isinstance(deck.kpi_chips, list)
    assert isinstance(deck.financials, list)
    assert isinstance(deck.valuation_table, list)
    assert isinstance(deck.peers, list)
    assert isinstance(deck.peer_names, list)
    assert isinstance(deck.risks, list)
    assert isinstance(deck.opportunities, list)
    assert isinstance(deck.scorecard, list)
    assert isinstance(deck.sections, list)


def test_realistic_fixture_p1_fixed():
    """P1: non-peer table columns no longer leak into peer_names (WP R.1)."""
    from shared.reports.extraction import _extract_deck_data

    brief = _load_fixture("realistic_quant")
    deck = _extract_deck_data(brief, "NVDA", "BUY", 0.85, "2024-01-15")
    peer_names_set = set(deck.peer_names)
    assert "Current" not in peer_names_set, "Financial header leaked into peer_names"
    assert "YoY Change" not in peer_names_set, "Financial header leaked into peer_names"
    assert "AMD" in peer_names_set, "AMD should be in peer_names"
    assert "INTC" in peer_names_set, "INTC should be in peer_names"
    assert len(deck.peer_names) == 2, "Only AMD and INTC should be peer names"
