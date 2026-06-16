"""Tests for the new analytics and reviewer Pydantic models."""

from shared.agent_models import (
    AnalyticsAgentOutput,
    AnomalyReport,
    ChartPayload,
    ConfidenceBreakdown,
    ContradictionFlag,
    ForecastResult,
    RecommendationValidation,
    ReviewerAgentOutput,
    SourceVerification,
    StatisticalSummary,
    TrendAnalysis,
    ValidatedAgentOutputs,
)


class TestAnalyticsModels:
    def test_trend_analysis_defaults(self):
        t = TrendAnalysis()
        assert t.trend_direction == "neutral"
        assert t.trend_strength == 0.0
        assert t.ma_crossover_signal is None
        assert t.supporting_indicators == []

    def test_trend_analysis_full(self):
        t = TrendAnalysis(
            trend_direction="bullish",
            ma_crossover_signal="golden_cross",
            momentum_shift="accelerating",
            trend_strength=0.85,
            supporting_indicators=["sma_bullish", "macd_bullish"],
        )
        assert t.trend_direction == "bullish"
        assert t.trend_strength == 0.85

    def test_forecast_result(self):
        f = ForecastResult(
            method="exponential_smoothing",
            horizon_days=30,
            forecast_prices=[150.0, 151.0],
            forecast_dates=["2025-02-01", "2025-02-02"],
            confidence_lower=[148.0, 147.0],
            confidence_upper=[152.0, 155.0],
            mape=3.5,
        )
        assert len(f.forecast_prices) == 2
        assert f.mape == 3.5

    def test_chart_payload(self):
        c = ChartPayload(
            chart_type="candlestick",
            labels=["2025-01-01"],
            datasets=[{"data": [{"close": 150}]}],
            annotations=[{"note": "test"}],
        )
        assert c.chart_type == "candlestick"

    def test_statistical_summary(self):
        s = StatisticalSummary(
            return_distribution="leptokurtic",
            skewness=0.5,
            kurtosis=3.5,
            jarque_bera_pvalue=0.01,
            correlations={"SPY": 0.85},
            regression_beta=1.2,
            regression_r_squared=0.72,
        )
        assert s.skewness == 0.5
        assert s.correlations["SPY"] == 0.85

    def test_anomaly_report(self):
        a = AnomalyReport(
            price_anomalies=[{"date": "2025-01-01", "z_score": 3.0}],
            volume_anomalies=[{"date": "2025-01-02", "z_score": 3.5}],
            fundamental_anomalies=["PE ratio 150 outside range"],
            anomaly_count=3,
            severity="medium",
        )
        assert a.anomaly_count == 3
        assert a.severity == "medium"

    def test_analytics_agent_output_defaults(self):
        o = AnalyticsAgentOutput(ticker="AAPL")
        assert o.ticker == "AAPL"
        assert o.analytics_signal == "neutral"
        assert o.analytics_confidence == 0.0
        assert o.trend_analysis is None
        assert o.charts == []

    def test_analytics_agent_output_full(self):
        o = AnalyticsAgentOutput(
            ticker="NVDA",
            trend_analysis=TrendAnalysis(trend_direction="bullish", trend_strength=0.8),
            forecast=ForecastResult(forecast_prices=[100.0]),
            charts=[ChartPayload(chart_type="line")],
            statistical_summary=StatisticalSummary(skewness=0.1),
            anomalies=AnomalyReport(severity="low"),
            analytics_confidence=0.85,
            analytics_signal="bullish",
        )
        assert o.analytics_confidence == 0.85
        assert o.trend_analysis.trend_direction == "bullish"


class TestReviewerModels:
    def test_contradiction_flag(self):
        c = ContradictionFlag(
            agents=["Quant", "Analytics"],
            field="recommendation vs trend",
            description="Quant BUY but bearish trend",
            severity="high",
        )
        assert c.severity == "high"
        assert len(c.agents) == 2

    def test_source_verification(self):
        s = SourceVerification(
            agent_name="Quant Analysis Agent",
            claims_checked=5,
            claims_verified=4,
            verification_rate=0.8,
            unverified_claims=["Sharpe ratio out of range"],
        )
        assert s.verification_rate == 0.8
        assert len(s.unverified_claims) == 1

    def test_confidence_breakdown(self):
        c = ConfidenceBreakdown(
            agent_scores={"quant": 0.8, "rag": 0.7},
            agreement_score=0.75,
            data_quality_score=0.85,
            meta_confidence=0.78,
        )
        assert c.meta_confidence == 0.78
        assert c.agent_scores.quant == 0.8

    def test_recommendation_validation(self):
        r = RecommendationValidation(
            recommendation="BUY",
            evidence_supports=True,
            supporting_evidence=["Golden cross", "DCF upside"],
            contradicting_evidence=["Bearish macro"],
            evidence_strength="moderate",
        )
        assert r.recommendation == "BUY"
        assert r.evidence_supports is True

    def test_reviewer_agent_output_defaults(self):
        o = ReviewerAgentOutput(ticker="AAPL")
        assert o.verdict == "HOLD"
        assert o.review_confidence == 0.0
        assert o.contradictions == []
        assert o.flags == []

    def test_reviewer_agent_output_full(self):
        o = ReviewerAgentOutput(
            ticker="NVDA",
            verdict="BUY",
            review_summary="Strong alignment across agents.",
            contradictions=[ContradictionFlag(agents=["A", "B"], field="x", description="y", severity="low")],
            source_verifications=[SourceVerification(agent_name="Quant", claims_checked=2, claims_verified=2, verification_rate=1.0)],
            confidence_breakdown=ConfidenceBreakdown(meta_confidence=0.85),
            recommendation_validation=RecommendationValidation(recommendation="BUY"),
            flags=["High anomaly risk"],
            review_confidence=0.82,
        )
        assert o.verdict == "BUY"
        assert len(o.contradictions) == 1
        assert o.review_confidence == 0.82


class TestValidatedAgentOutputs:
    def test_has_all_agents_false_by_default(self):
        v = ValidatedAgentOutputs(ticker="AAPL")
        assert v.has_all_agents is False

    def test_has_all_agents_true(self):
        from shared.agent_models import (
            MarketContextOutput,
            QuantAgentOutput,
            RAGAgentOutput,
        )

        v = ValidatedAgentOutputs(
            ticker="AAPL",
            quant=QuantAgentOutput(ticker="AAPL"),
            rag=RAGAgentOutput(ticker="AAPL"),
            market_context=MarketContextOutput(ticker="AAPL"),
            analytics=AnalyticsAgentOutput(ticker="AAPL"),
            reviewer=ReviewerAgentOutput(ticker="AAPL"),
        )
        assert v.has_all_agents is True

    def test_has_all_agents_false_missing_reviewer(self):
        from shared.agent_models import (
            MarketContextOutput,
            QuantAgentOutput,
            RAGAgentOutput,
        )

        v = ValidatedAgentOutputs(
            ticker="AAPL",
            quant=QuantAgentOutput(ticker="AAPL"),
            rag=RAGAgentOutput(ticker="AAPL"),
            market_context=MarketContextOutput(ticker="AAPL"),
            analytics=AnalyticsAgentOutput(ticker="AAPL"),
        )
        assert v.has_all_agents is False
