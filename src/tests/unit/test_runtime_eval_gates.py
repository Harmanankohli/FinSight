"""Unit tests for runtime_eval gates: dedup, burst limit, kill switch."""

from shared import runtime_eval


def _reset():
    """Reset module-level state between tests."""
    runtime_eval._dedup_cache.clear()
    runtime_eval._burst_window_start = 0.0
    runtime_eval._burst_count = 0
    runtime_eval._eval_failure_count = 0
    runtime_eval._eval_circuit_open = False


def test_dedup_seen_blocks_repeated_pair(monkeypatch):
    _reset()
    # First call should be allowed, second blocked
    assert runtime_eval._dedup_seen("query A", "response X") is False
    assert runtime_eval._dedup_seen("query A", "response X") is True
    # Different response → not blocked
    assert runtime_eval._dedup_seen("query A", "response Y") is False


def test_burst_limiter_caps_per_minute(monkeypatch):
    _reset()
    monkeypatch.setattr(runtime_eval, "EVAL_BURST_LIMIT", 3)
    assert runtime_eval._burst_ok() is True
    assert runtime_eval._burst_ok() is True
    assert runtime_eval._burst_ok() is True
    assert runtime_eval._burst_ok() is False  # 4th in same window — blocked


def test_burst_limiter_disabled_when_zero(monkeypatch):
    _reset()
    monkeypatch.setattr(runtime_eval, "EVAL_BURST_LIMIT", 0)
    for _ in range(100):
        assert runtime_eval._burst_ok() is True


def test_gate_ok_respects_runtime_disabled(monkeypatch):
    _reset()
    monkeypatch.setattr(runtime_eval, "EVAL_RUNTIME_DISABLED", True)
    assert runtime_eval._gate_ok("q", "r" * 200, "orchestrator") is False


def test_gate_ok_respects_open_circuit(monkeypatch):
    _reset()
    monkeypatch.setattr(runtime_eval, "_eval_circuit_open", True)
    assert runtime_eval._gate_ok("q", "r" * 200, "orchestrator") is False


def test_gate_ok_allows_normal_call(monkeypatch):
    _reset()
    monkeypatch.setattr(runtime_eval, "EVAL_RUNTIME_DISABLED", False)
    monkeypatch.setattr(runtime_eval, "EVAL_BURST_LIMIT", 10)
    assert runtime_eval._gate_ok("query unique 1", "response X", "rag") is True


def test_score_quant_deterministic_catches_missing_signals():
    result = {
        "signal_scores": {"risk_quality": 0.1},  # missing 7 groups
        "recommendation": "BUY",
        "confidence_score": 0.5,
    }
    checks = runtime_eval.score_quant_deterministic(result)
    assert checks["passed"] is False
    assert checks["signal_groups_complete"] is False


def test_score_quant_deterministic_accepts_full_payload():
    result = {
        "metrics": {
            "signal_scores": {
                "risk_quality": 0.5,
                "dcf_value": 0.3,
                "fundamental_value": 0.0,
                "fundamental_quality": 0.1,
                "technicals_trend": 0.2,
                "technicals_momentum": 0.4,
                "peer_positioning": 0.1,
                "behavioral": 0.0,
            },
            "quant_confidence": 0.7,
        },
        "options_signals": {"pc_vol": 0.5},
        "insider_signals": {"count": 3},
        "positioning": {"consensus_score": 1.5},
        "monte_carlo": {"p50": 200.0, "prob_profit": 0.6},
        "peer_comparison": {"rankings": {"pe_rank": 2}},
        "recommendation": "BUY",
    }
    checks = runtime_eval.score_quant_deterministic(result)
    assert checks["passed"] is True


def test_score_quant_deterministic_rejects_bad_recommendation():
    result = {
        "signal_scores": {
            "risk_quality": 0,
            "dcf": 0,
            "fund_value": 0,
            "fund_quality": 0,
            "tech_trend": 0,
            "tech_momentum": 0,
            "peer_positioning": 0,
            "behavioral": 0,
        },
        "options_signals": {},
        "insider_signals": {},
        "positioning": {},
        "recommendation": "MAYBE",  # invalid
        "confidence_score": 0.5,
    }
    checks = runtime_eval.score_quant_deterministic(result)
    assert checks["recommendation_valid"] is False
    assert checks["passed"] is False
