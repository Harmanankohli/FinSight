# FinSight - Codebase Verified Improvement Plan

Version: June 2026

This document only includes findings verified against the actual source code.

---

# Executive Summary

Current System Assessment

The system is considerably more mature than initially assumed.

Major observations:

* Financial metrics are accurate.
* DCF implementation is structurally correct.
* Net debt adjustment is already implemented.
* DCF/Monte Carlo divergence detection already exists.
* Most remaining issues are consistency, explainability, and reporting quality issues rather than valuation engine bugs.

Priority should shift from:

"Fix valuation calculations"

to:

"Improve consistency, transparency, and report quality"

---

# P0 - Verified Issues

---

# P0.1 RSI Methodology Inconsistency

## Verified Finding

Two different RSI calculations exist.

### Technical Analysis Node

File:

src/quant/nodes/technical.py

Uses:

EMA-based RSI calculation

---

### Trend Analysis Node

File:

src/analytics/nodes/trend.py

Uses:

Simple-average RSI recomputation

---

## Impact

Same report may show:

RSI = 51

while trend scoring uses:

RSI = 39

This is not a stale-cache issue.

It is a methodology mismatch.

---

## Recommended Fix

Make trend analysis consume:

state["technicals"]["rsi_14"]

instead of recalculating RSI.

---

## Benefit

Single source of truth.

Consistent technical analysis.

---

# P0.2 MACD Signal Investigation

## Verified Finding

Original diagnosis was incorrect.

technical.py already uses:

macd_histogram > 0

which is mathematically valid.

---

## Open Question

Report shows:

MACD = 1.207

Signal = 3.251

Histogram = -2.044

but signal rendered as:

Bullish

---

## Investigation Required

Trace:

technical.py

↓

trend.py

↓

summary generation

↓

report rendering

Determine where bullish classification changes.

---

## Recommended Action

Add debug logging:

MACD

Signal

Histogram

Rendered Signal

during report generation.

---

## Expected Outcome

Locate rendering or aggregation inconsistency.

---

# P0.3 Reviewer Contradiction Deduplication

## Verified Finding

Duplicate contradiction categories appear.

Examples:

high confidence_breakdown

low confidence_score

multiple verification statements

---

## Files

src/reviewer/executor.py

src/reviewer/tools/validation.py

src/reviewer/tools/contradiction.py

---

## Recommended Fix

Before adding contradiction:

category_key = (
contradiction_type,
topic
)

if category_key not in seen:
add()

---

## Benefit

Cleaner reviewer output.

Higher trust.

---

# P1 - Transparency Improvements

---

# P1.1 Export Full DCF Audit Trail

## Verified Finding

These values are computed internally:

risk_free_rate

beta

cost_of_equity

cost_of_debt

tax_rate

but are not exported.

---

## File

src/quant/nodes/dcf.py

---

## Add To Output

dcf_assumptions = {

```
"risk_free_rate": risk_free_rate,

"beta": beta,

"cost_of_equity": cost_of_equity,

"cost_of_debt": after_tax_cod,

"tax_rate": tax_rate,

"shares_outstanding": shares_outstanding,

"net_debt": net_debt,

"fcf_used": latest_fcf,
```

}

---

## Benefit

Fully auditable DCF.

Better interview discussion.

---

# P1.2 DCF Assumption Explanation

## Problem

Users see:

DCF Fair Value = $131

but do not know why.

---

## Recommendation

Add report section:

DCF Assumptions

Risk-Free Rate

Beta

Cost of Equity

Cost of Debt

Tax Rate

Growth Rate

Terminal Growth

Shares Outstanding

Net Debt

FCF Used

---

## Benefit

Explainability.

Trust.

---

# P1.3 Recommendation Driver Summary

## Problem

Current report requires users to infer:

why HOLD was selected.

---

## Recommendation

Generate:

Recommendation Drivers

Positive Signals

Negative Signals

Most Influential Factors

---

## Example

Positive

* Revenue Growth
* Margin Strength
* Technical Trend

Negative

* DCF Downside
* Valuation Premium
* Elevated Anomalies

---

## Benefit

More analyst-like reports.

---

# P2 - Model Enhancements

---

# P2.1 DCF Sensitivity Matrix

## Current State

Not implemented.

---

## Add

Sensitivity grid:

Terminal Growth

2.0%

2.5%

3.0%

3.5%

WACC

8%

9%

10%

11%

12%

---

## Output

DCF Sensitivity Matrix

---

## Benefit

Shows assumption risk.

Industry-standard valuation output.

---

# P2.2 DCF Confidence Redesign

## Current State

Confidence based largely on:

assumption reliability

input availability

model health

---

## Issue

Users may interpret:

84% confidence

as

84% probability fair value is correct

which is misleading.

---

## Recommendation

Rename

DCF Confidence

to

Model Reliability

or

Valuation Reliability

---

## Benefit

Clearer interpretation.

---

# P2.3 Enhanced Peer Selection

## Verified Finding

Peer logic currently lives in:

src/quant/nodes/summary.py

src/shared/peer_sets.py

---

## Recommendation

Add filtering layer:

same sector

similar market cap

positive revenue

positive operating margin

---

## Goal

Prevent weak peer matches.

---

# P3 - Interview Showcase Features

---

# P3.1 DCF Sensitivity Visualization

Generate heatmap/table showing:

WACC sensitivity

Terminal growth sensitivity

Intrinsic value impact

---

# P3.2 Report Health Score

Score components:

Data Quality

Valuation Consistency

Source Coverage

Forecast Stability

---

# P3.3 Explainable Recommendation Layer

Output:

Top 5 factors driving recommendation

Factor contribution scores

Agent influence weighting

---

# Recommended Implementation Order

Sprint 1

1. RSI single-source-of-truth
2. MACD signal investigation
3. Reviewer contradiction deduplication

Expected effort:
1 day

---

Sprint 2

1. Export DCF audit trail
2. Add DCF assumptions section
3. Recommendation driver summary

Expected effort:
2–3 days

---

Sprint 3

1. Sensitivity matrix
2. Confidence terminology redesign
3. Peer filtering improvements

Expected effort:
3–4 days

---

# Final Recommendation

Do not spend more time changing DCF formulas.

The valuation engine is already reasonably robust.

The highest-value improvements are:

1. Consistency across nodes
2. Explainability of outputs
3. Transparency of assumptions
4. Cleaner reviewer analysis

These changes will improve both report quality and interview value more than additional valuation complexity.
