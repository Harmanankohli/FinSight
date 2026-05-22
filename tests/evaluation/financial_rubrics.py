"""Financial-domain custom RAGAS metrics.

Three rubrics tuned for investment research quality:
- citation_quality: cites specific filing sections, dates, figures
- risk_disclosure: explicitly acknowledges investment risks
- recommendation_clarity: 1-5 scale for BUY/HOLD/SELL with supporting evidence
"""

from __future__ import annotations

try:
    from ragas.metrics import AspectCritic, RubricsBasedScoring

    citation_quality = AspectCritic(
        name="citation_quality",
        definition=(
            "Does the response cite specific filing sections, dates, dollar amounts, "
            "or percentages from source documents rather than making generic claims? "
            "A good response includes at least one specific figure or filing reference."
        ),
    )

    risk_disclosure = AspectCritic(
        name="risk_disclosure",
        definition=(
            "Does the response explicitly acknowledge key investment risks "
            "(regulatory, competitive, market, operational) and not present only upside? "
            "A balanced response must mention at least one material risk."
        ),
    )

    recommendation_clarity = RubricsBasedScoring(
        name="recommendation_clarity",
        rubrics={
            "score1_description": "No clear BUY/HOLD/SELL signal present.",
            "score2_description": "Signal present but rationale is absent or vague.",
            "score3_description": "Signal with rationale but supported by only one data point.",
            "score4_description": "Signal with rationale supported by two or more data points from different sources.",
            "score5_description": "Clear signal with specific supporting evidence from at least two agent analyses (quant, filing, or sentiment) with cited figures.",
        },
    )

    FINANCIAL_METRICS = [citation_quality, risk_disclosure, recommendation_clarity]

except ImportError:
    FINANCIAL_METRICS = []
