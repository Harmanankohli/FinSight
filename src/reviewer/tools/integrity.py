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

    # ── Quant: Monte Carlo ──
    mc = quant.get("monte_carlo") or {}
    prob_profit = mc.get("prob_profit")
    if prob_profit is not None:
        if not (0.0 <= prob_profit <= 1.0):
            alerts.append(
                {
                    "agent": "quant",
                    "metric": "monte_carlo.prob_profit",
                    "severity": "critical",
                    "message": f"prob_profit={prob_profit} outside [0, 1]",
                }
            )
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

    # ── Quant: Sharpe ratio ──
    metrics = quant.get("metrics") or {}
    sharpe = metrics.get("sharpe_ratio")
    if isinstance(sharpe, (int, float)) and not (-5 <= sharpe <= 5):
        alerts.append(
            {
                "agent": "quant",
                "metric": "metrics.sharpe_ratio",
                "severity": "critical",
                "message": f"Sharpe ratio {sharpe:.2f} outside plausible range [-5, 5]",
            }
        )

    # ── Quant: VaR ──
    var_95 = metrics.get("var_95_daily")
    if var_95 is not None and not (0 <= var_95 <= 1):
        alerts.append(
            {
                "agent": "quant",
                "metric": "metrics.var_95_daily",
                "severity": "critical",
                "message": f"VaR(95%) {var_95:.4f} outside [0, 1]",
            }
        )

    # ── Quant: Beta ──
    beta = metrics.get("beta")
    if isinstance(beta, (int, float)) and not (-3 <= beta <= 6):
        alerts.append(
            {
                "agent": "quant",
                "metric": "metrics.beta",
                "severity": "warning",
                "message": f"Beta {beta:.2f} outside typical range [-3, 6]",
            }
        )

    # ── Quant: RSI ──
    technicals = quant.get("technicals") or {}
    rsi = technicals.get("rsi_14") or technicals.get("rsi")
    if isinstance(rsi, (int, float)) and not (0 <= rsi <= 100):
        alerts.append(
            {
                "agent": "quant",
                "metric": "technicals.rsi",
                "severity": "critical",
                "message": f"RSI {rsi:.1f} outside [0, 100]",
            }
        )

    # ── Quant: recommendation vs quant_signal direction ──
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

    # ── Quant: MAPE if present ──
    mape = _safe_get(quant, "forecast", "mape", default=None)
    if isinstance(mape, (int, float)) and mape < 0:
        alerts.append(
            {
                "agent": "quant",
                "metric": "forecast.mape",
                "severity": "critical",
                "message": f"MAPE is negative ({mape:.2f}) — mathematically impossible",
            }
        )

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

    # ── Analytics: confidence ──
    a_conf = analytics.get("analytics_confidence")
    if isinstance(a_conf, (int, float)) and not (0.0 <= a_conf <= 1.0):
        alerts.append(
            {
                "agent": "analytics",
                "metric": "analytics_confidence",
                "severity": "critical",
                "message": f"Analytics confidence {a_conf} outside [0, 1]",
            }
        )

    # ── Analytics: anomaly count ──
    anomalies = analytics.get("anomalies") or {}
    a_count = anomalies.get("anomaly_count")
    if isinstance(a_count, (int, float)) and a_count < 0:
        alerts.append(
            {
                "agent": "analytics",
                "metric": "anomalies.anomaly_count",
                "severity": "critical",
                "message": f"Anomaly count is negative ({a_count})",
            }
        )

    # ── Analytics: forecast MAPE ──
    forecast = analytics.get("forecast") or {}
    a_mape = forecast.get("mape")
    if isinstance(a_mape, (int, float)) and a_mape < 0:
        alerts.append(
            {
                "agent": "analytics",
                "metric": "forecast.mape",
                "severity": "critical",
                "message": f"Forecast MAPE is negative ({a_mape:.2f}) — mathematically impossible",
            }
        )

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
