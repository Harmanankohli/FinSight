"""Metric integrity validation tool that checks for missing or anomalous financial data."""

def _safe_get(d: dict, *keys, default=None):
    for k in keys:
        if isinstance(d, dict):
            d = d.get(k, {})
        else:
            return default
    return d if d is not None else default


def validate_metric_integrity(agent_outputs: dict) -> list[dict]:
    """Pre-reviewer gate: checks mathematical invariants and plausible ranges.

    Returns a list of alerts, each with severity (critical / warning / info).
    """
    alerts: list[dict] = []
    quant = agent_outputs.get("quant", {})
    analytics = agent_outputs.get("analytics", {})

    # ── Quant: DCF upside consistency ──
    dcf = quant.get("dcf_valuation") or {}
    intrinsic = dcf.get("intrinsic_value") or dcf.get("fair_value")
    current = dcf.get("current_price")
    upside = dcf.get("upside_pct")
    if intrinsic and current and upside is not None:
        expected = (intrinsic - current) / current * 100
        if abs(expected - upside) > 1.0:
            alerts.append(
                {
                    "agent": "quant",
                    "metric": "dcf_valuation.upside_pct",
                    "severity": "critical",
                    "message": f"Reported upside {upside:.1f}% != calculated {expected:.1f}% from intrinsic={intrinsic:.2f}, price={current:.2f}",
                }
            )

    # ── Quant: Monte Carlo cross-field consistency ──
    # (prob_profit range [0,1] is enforced by MonteCarloResult Pydantic model)
    mc = quant.get("monte_carlo") or {}
    prob_profit = mc.get("prob_profit")
    if prob_profit is not None:
        p50 = mc.get("p50")
        if p50 is not None and current is not None:
            if p50 > current and prob_profit < 0.4:
                alerts.append(
                    {
                        "agent": "quant",
                        "metric": "monte_carlo.prob_profit vs p50",
                        "severity": "warning",
                        "message": f"Median outcome p50={p50:.2f} > current price {current:.2f} but prob_profit={prob_profit:.2f}",
                    }
                )
            elif p50 < current and prob_profit > 0.7:
                alerts.append(
                    {
                        "agent": "quant",
                        "metric": "monte_carlo.prob_profit vs p50",
                        "severity": "warning",
                        "message": f"Median outcome p50={p50:.2f} < current price {current:.2f} but prob_profit={prob_profit:.2f}",
                    }
                )

    # Range checks for sharpe_ratio, var_95_daily, beta, rsi are enforced
    # by Pydantic Field constraints on QuantRiskMetrics / TechnicalIndicators.

    # ── Quant: recommendation vs quant_signal direction ──
    metrics = quant.get("metrics") or {}
    rec = quant.get("recommendation")
    signal = metrics.get("quant_signal")
    if rec and signal:
        if rec == "BUY" and signal == "bearish":
            alerts.append(
                {
                    "agent": "quant",
                    "metric": "recommendation vs quant_signal",
                    "severity": "warning",
                    "message": f"Recommendation is BUY but quant_signal is bearish",
                }
            )
        elif rec == "SELL" and signal == "bullish":
            alerts.append(
                {
                    "agent": "quant",
                    "metric": "recommendation vs quant_signal",
                    "severity": "warning",
                    "message": f"Recommendation is SELL but quant_signal is bullish",
                }
            )

    # ── Quant: fundamentals ──
    fundamentals = quant.get("fundamentals") or {}
    pe = fundamentals.get("trailing_pe")
    if isinstance(pe, (int, float)) and pe < 0:
        alerts.append(
            {
                "agent": "quant",
                "metric": "fundamentals.trailing_pe",
                "severity": "info",
                "message": f"Negative P/E ratio ({pe:.1f}) — company may be unprofitable",
            }
        )

    # MAPE >= 0 is enforced by ForecastResult Pydantic model.

    # ── Quant: stress test vs recommendation ──
    stress = quant.get("stress_test") or {}
    worst = stress.get("worst_case_return") or stress.get("worst_case")
    if isinstance(worst, (int, float)) and rec == "BUY" and worst < -0.40:
        alerts.append(
            {
                "agent": "quant",
                "metric": "stress_test vs recommendation",
                "severity": "warning",
                "message": f"Recommendation is BUY but stress test worst case is {worst:.0%}",
            }
        )

    # analytics_confidence [0,1], anomaly_count >= 0, and forecast MAPE >= 0
    # are enforced by Pydantic Field constraints on AnalyticsAgentOutput,
    # AnomalyReport, and ForecastResult respectively.

    # ── Analytics: momentum range ──
    trend = analytics.get("trend_analysis") or {}
    momentum = trend.get("momentum_shift")
    if isinstance(momentum, (int, float)) and abs(momentum) > 100:
        alerts.append(
            {
                "agent": "analytics",
                "metric": "trend_analysis.momentum_shift",
                "severity": "warning",
                "message": f"Momentum shift {momentum:.1f}% seems implausibly large",
            }
        )

    # ── Missing expected agents ──
    for agent_key, label in [("quant", "Quant"), ("rag", "RAG"), ("analytics", "Analytics")]:
        if not agent_outputs.get(agent_key):
            alerts.append(
                {
                    "agent": agent_key,
                    "metric": "(entire output)",
                    "severity": "critical",
                    "message": f"{label} agent output is missing — review will be incomplete",
                }
            )

    return alerts
