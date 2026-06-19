# FinSight DCF & Valuation Improvement Plan
Version: June 2026
Priority: Production Hardening

---

# Executive Summary

After reviewing:

- Latest report outputs
- Validation against external market data
- Actual DCF implementation
- Financial data pipeline

The core DCF engine is NOT fundamentally broken.

Most discrepancies come from:

1. Missing Net Debt adjustment
2. Lack of DCF assumption transparency
3. Static Risk-Free Rate
4. Potential stale growth inputs
5. Basic shares instead of diluted shares
6. Lack of valuation diagnostics

The goal of this document is to improve valuation realism, transparency, and institutional-grade report quality.

---

# Priority Matrix

| Priority | Task | Impact | Effort |
|-----------|--------|----------|----------|
| P0 | Net Debt Adjustment | Very High | Low |
| P0 | DCF Assumption Transparency | Very High | Low |
| P1 | Live Risk-Free Rate | High | Low |
| P1 | Diluted Shares Support | Medium | Low |
| P1 | Growth Input Diagnostics | High | Low |
| P1 | DCF Validation Framework | High | Medium |
| P2 | Sector-Specific Terminal Growth | Medium | Medium |
| P2 | Multi-Scenario DCF | High | Medium |
| P2 | DCF Confidence Score | Medium | Medium |

---

# TASK 1: Net Debt Adjustment

## Priority

P0

---

## Problem

Current implementation:

```python
intrinsic_value = enterprise_value / shares_outstanding
```

This assumes:

```text
Enterprise Value = Equity Value
```

which is incorrect.

DCF theory:

```text
Enterprise Value
- Net Debt
= Equity Value

Equity Value
/ Shares Outstanding
= Intrinsic Value Per Share
```

---

## File

```text
src/quant/nodes/dcf.py
```

---

## Current

```python
enterprise_value = pv_fcf + pv_terminal

intrinsic_value = (
    enterprise_value /
    shares_outstanding
)
```

---

## Replace With

```python
enterprise_value = pv_fcf + pv_terminal

total_debt = info.get("totalDebt", 0) or 0
total_cash = info.get("totalCash", 0) or 0

net_debt = total_debt - total_cash

equity_value = (
    enterprise_value -
    net_debt
)

intrinsic_value = (
    equity_value /
    shares_outstanding
)
```

---

## Add To Output

```python
"net_debt": net_debt,
"equity_value": equity_value,
```

---

## Expected Impact

Likely increases AAPL intrinsic value.

Improves valuation accuracy.

---

# TASK 2: DCF Assumption Transparency

## Priority

P0

---

## Problem

Users see:

```text
DCF Fair Value = $132
```

but cannot see why.

---

## File

```text
src/quant/nodes/dcf.py
```

and report rendering layer.

---

## Add

```python
dcf_assumptions = {
    "fcf": latest_fcf,
    "growth_rate": growth_rate,
    "terminal_growth": terminal_growth,
    "wacc": wacc,
    "shares_outstanding": shares_outstanding,
    "net_debt": net_debt,
}
```

---

## Add To Report

### DCF Assumptions

```text
FCF Used: $129.1B

Growth Rate: 12.0%

Terminal Growth: 2.5%

WACC: 8.7%

Net Debt: -$50B

Shares Outstanding: 15.1B
```

---

## Benefit

Removes black-box valuation concerns.

---

# TASK 3: Live Risk-Free Rate

## Priority

P1

---

## Problem

Current:

```python
risk_free_rate = 0.043
```

Hardcoded.

---

## File

```text
src/quant/nodes/dcf.py
```

---

## Current

```python
risk_free_rate = 0.043
```

---

## Replace With

```python
risk_free_rate = (
    macro_data.get(
        "treasury_10y_yield",
        4.3
    ) / 100
)
```

Fallback:

```python
risk_free_rate = 0.043
```

---

## Benefit

Automatically adapts to rate cycles.

---

# TASK 4: Diluted Shares Support

## Priority

P1

---

## Problem

Current:

```python
sharesOutstanding
```

Usually basic shares.

---

## File

```text
src/quant/nodes/dcf.py
```

---

## Current

```python
shares_outstanding = (
    info.get("sharesOutstanding")
)
```

---

## Replace With

```python
shares_outstanding = (
    info.get(
        "impliedSharesOutstanding"
    )
    or info.get(
        "sharesOutstanding"
    )
)
```

---

## Benefit

More accurate per-share valuation.

---

# TASK 5: Growth Input Diagnostics

## Priority

P1

---

## Problem

Growth assumptions may be causing undervaluation.

No visibility currently.

---

## File

```text
src/quant/nodes/dcf.py
```

---

## Add Logging

```python
logger.info(
    f"""
    Revenue Growth={rg}
    Earnings Growth={eg}
    Final Growth={growth_rate}
    """
)
```

---

## Add To Report

### Growth Inputs

```text
Revenue Growth: 14.9%

Earnings Growth: 16.2%

Blended Growth: 15.4%
```

---

## Benefit

Makes DCF fully explainable.

---

# TASK 6: DCF Validation Framework

## Priority

P1

---

## New File

```text
src/reviewer/tools/dcf_validator.py
```

---

## Checks

### Check 1

```python
intrinsic_value /
current_price
```

---

### Warning

```text
DCF Value < 30% of Market Price
```

---

### Check 2

```python
intrinsic_value >
3 * current_price
```

---

### Warning

```text
DCF Value > 300% of Market Price
```

---

### Check 3

```python
fcf_age_days > 365
```

---

### Warning

```text
Financial Data Appears Stale
```

---

## Benefit

Detects unrealistic outputs automatically.

---

# TASK 7: Sector-Specific Terminal Growth

## Priority

P2

---

## Problem

Current:

```python
terminal_growth = 2.5%
```

for almost everything.

---

## Create

```python
TERMINAL_GROWTH = {
    "Technology": 0.03,
    "Communication Services": 0.03,
    "Consumer Defensive": 0.025,
    "Healthcare": 0.025,
    "Utilities": 0.02,
    "Energy": 0.02,
}
```

---

## Benefit

More realistic terminal assumptions.

---

# TASK 8: Multi-Scenario DCF

## Priority

P2

---

## Problem

Single valuation point.

Institutional research uses:

- Bear
- Base
- Bull

---

## Create

```python
bear_case
base_case
bull_case
```

---

## Example

### Bear

```text
Growth -25%
WACC +1%
```

### Base

```text
Current assumptions
```

### Bull

```text
Growth +25%
WACC -1%
```

---

## Output

```text
Bear Fair Value: $125

Base Fair Value: $155

Bull Fair Value: $195
```

---

## Benefit

Massively improves credibility.

---

# TASK 9: DCF Confidence Score

## Priority

P2

---

## Add

```python
confidence = (
    0.4 * data_quality +
    0.3 * forecast_stability +
    0.3 * assumption_reliability
)
```

---

## Output

```text
DCF Confidence: 78%
```

---

## Benefit

Users understand trust level.

---

# Recommended Implementation Order

## Sprint 1

### Implement

- Net Debt Adjustment
- DCF Assumption Transparency

Expected Impact:

```text
Valuation Quality
8.0 → 8.8
```

---

## Sprint 2

### Implement

- Live Risk-Free Rate
- Diluted Shares
- Growth Diagnostics
- DCF Validator

Expected Impact:

```text
8.8 → 9.2
```

---

## Sprint 3

### Implement

- Sector Growth Profiles
- Multi-Scenario DCF
- DCF Confidence

Expected Impact:

```text
9.2 → 9.5
```

---

# Final Recommendation

Implement ONLY Sprint 1 and Sprint 2 first.

Those four changes provide approximately 80% of the valuation improvement with less than 20% of the effort.

Do not add new agents, new LLM calls, or additional orchestration complexity until these valuation diagnostics are complete.