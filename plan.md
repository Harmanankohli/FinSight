# FinSight Metric Correction & Standardization Specification

Version: 1.0
Priority: High
Target: Quant Engine, Technical Analysis Engine, Valuation Engine

---

# Objective

Standardize all financial metric calculations to institutional-grade implementations and eliminate inconsistencies across agents.

---

# P0 - Critical Fixes

## P0.1 RSI Standardization

### Problem

Multiple RSI implementations currently exist.

Observed issues:

* Trend analysis computes RSI independently.
* Technical indicators compute RSI separately.
* Different smoothing methods produce different values.

This causes report inconsistencies.

---

### Required Implementation

Create a single source of truth:

```python
compute_rsi_wilder(
    prices: pd.Series,
    period: int = 14
) -> float
```

Use Wilder's smoothing:

```text
Gain = max(change, 0)
Loss = max(-change, 0)

AvgGain_t =
((PrevAvgGain * 13) + CurrentGain) / 14

AvgLoss_t =
((PrevAvgLoss * 13) + CurrentLoss) / 14

RS = AvgGain / AvgLoss

RSI = 100 - (100 / (1 + RS))
```

---

### Requirements

All modules must consume:

```python
state["technicals"]["rsi_14"]
```

No module may recompute RSI.

---

### Validation

Range:

```text
0 <= RSI <= 100
```

Assertions:

```python
assert 0 <= rsi <= 100
```

---

### Interpretation Bands

| Range  | Meaning    |
| ------ | ---------- |
| 0-30   | Oversold   |
| 30-70  | Neutral    |
| 70-100 | Overbought |

---

# P1 - Sharpe Ratio Improvement

## Current Issue

Some implementations may use raw returns.

Institutional Sharpe requires excess returns.

---

## Required Formula

```text
Excess Return =
Asset Return - Risk Free Return
```

```text
Sharpe =
Mean(Excess Returns)
/
Std(Excess Returns)
```

Annualized:

```text
Sharpe_Annual =
Sharpe * sqrt(252)
```

---

## Function

```python
compute_sharpe_ratio(
    returns: np.ndarray,
    risk_free_rate: float
)
```

---

## Validation

```python
assert np.isfinite(sharpe)
```

---

## Expected Range

```text
(-∞, +∞)
```

Typical:

| Value | Meaning     |
| ----- | ----------- |
| <0    | Poor        |
| 0-1   | Weak        |
| 1-2   | Good        |
| 2-3   | Excellent   |
| >3    | Exceptional |

---

# P1 - Beta Calculation Upgrade

## Current Issue

Short daily samples create unstable beta values.

---

## Required Formula

```text
Beta =
Covariance(
    Asset Returns,
    Market Returns
)
/
Variance(
    Market Returns
)
```

---

## Required Window

Preferred:

```text
104 weeks
```

Fallback:

```text
5 years monthly returns
```

Minimum:

```text
252 trading days
```

---

## Function

```python
compute_beta(
    asset_returns,
    benchmark_returns
)
```

---

## Validation

```python
assert np.isfinite(beta)
```

---

## Range

```text
(-∞, +∞)
```

---

# P1 - WACC Modernization

## Current Issue

Risk-free rate is hardcoded.

This creates stale valuations.

---

## Required Change

Risk-free rate must be fetched dynamically.

Preferred source:

US Treasury 10-Year Yield

Store:

```python
state["macro"]["risk_free_rate"]
```

---

## Formula

```text
CostOfEquity =
Rf + Beta * MarketRiskPremium
```

```text
WACC =
(
E/(D+E)
*
CostOfEquity
)
+
(
D/(D+E)
*
CostOfDebt
*
(1-TaxRate)
)
```

---

## Validation

```text
0% <= WACC <= 25%
```

Alert if outside range.

---

# P1 - DCF Growth Model Rewrite

## Current Issue

Current formula:

```text
0.6 Revenue Growth
+
0.4 Earnings Growth
```

is not finance-standard.

---

## Required Change

Use historical CAGR.

Preferred:

```text
5-Year FCF CAGR
```

Fallback:

```text
5-Year Revenue CAGR
```

---

## Formula

```text
CAGR =
(
EndingValue
/
BeginningValue
)
^(1/N)
-
1
```

---

## Growth Constraints

```text
-20% <= Growth <= 25%
```

Clamp values outside range.

---

## Storage

```python
state["valuation"]["projected_growth"]
```

---

# P1 - Terminal Growth Constraints

## Required Rule

Terminal Growth must never exceed:

```text
Long-Term GDP Growth
```

Implementation:

```python
terminal_growth = min(
    calculated_growth,
    0.03
)
```

---

## Range

```text
0% - 3%
```

Default:

```text
2.5%
```

---

# P2 - Monte Carlo Enhancement

## Current Limitation

GBM ignores:

* Earnings gaps
* Black swan events
* Jump risk

---

## Current Model

Keep:

```text
dS =
μSdt
+
σSdW
```

---

## Future Upgrade

Add:

### Option A

Jump Diffusion

```text
Merton Jump Diffusion
```

### Option B

Historical Bootstrap

```python
np.random.choice(
    historical_returns
)
```

---

# P2 - VaR Standardization

## Historical VaR

```text
VaR95 =
5th percentile return
```

Implementation:

```python
np.percentile(
    returns,
    5
)
```

---

## Validation

Expected:

```text
-20% <= VaR <= 0%
```

---

# P2 - CVaR Standardization

## Formula

```text
CVaR =
Mean(
Returns <= VaR95
)
```

---

## Validation

Always enforce:

```python
assert cvar <= var
```

---

# P3 - New Metrics To Add

## Sortino Ratio

### Formula

```text
(
Mean Return
-
Risk Free Rate
)
/
Downside Deviation
```

---

### Function

```python
compute_sortino_ratio()
```

---

### Range

```text
(-∞,+∞)
```

---

## Calmar Ratio

### Formula

```text
CAGR
/
Maximum Drawdown
```

---

### Function

```python
compute_calmar_ratio()
```

---

### Range

```text
(-∞,+∞)
```

---

## Alpha

### Formula

```text
Alpha =
Actual Return
-
Expected CAPM Return
```

Where:

```text
Expected CAPM Return =
Rf +
Beta *
(Market Return - Rf)
```

---

### Function

```python
compute_alpha()
```

---

### Range

```text
(-∞,+∞)
```

---

## Information Ratio

### Formula

```text
Active Return
/
Tracking Error
```

Where:

```text
Tracking Error =
Std(
Portfolio -
Benchmark
)
```

---

### Function

```python
compute_information_ratio()
```

---

### Range

```text
(-∞,+∞)
```

---

# Validation Layer

Every metric should expose:

```python
{
    "value": float,
    "methodology": str,
    "min_valid": float,
    "max_valid": float,
    "status": str
}
```

Example:

```python
{
    "value": 1.72,
    "methodology": "Annualized Excess Return Sharpe",
    "min_valid": -999,
    "max_valid": 999,
    "status": "VALID"
}
```

---

# Acceptance Criteria

## Quant Engine

* Single RSI implementation
* Dynamic risk-free rate
* CAGR-based growth projections
* Sharpe uses excess returns
* Beta uses longer windows

## New Metrics

* Sortino Ratio
* Calmar Ratio
* Alpha
* Information Ratio

## Validation

* All metrics contain min/max validation
* Out-of-range values generate warnings
* Report displays methodology used
