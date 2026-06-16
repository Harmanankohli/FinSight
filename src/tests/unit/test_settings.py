"""Tests for shared/settings.py — Settings class, env precedence, back-compat, validate_runtime."""

import pytest

from shared.settings import Settings, get_settings, reset_settings_for_tests


@pytest.fixture(autouse=True)
def _reset():
    reset_settings_for_tests()
    yield
    reset_settings_for_tests()


# ── Env precedence ────────────────────────────────────────────────────────────


def test_default_values(monkeypatch):
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv("_ENV_FILE", "")  # prevent .env file from being read
    s = Settings(_env_file=None)
    assert s.llm_model == "qwen/qwen3-30b-a3b-2507"
    assert s.env == "development"
    assert s.auth_enabled is False
    assert s.agent_port_rag == 8002


def test_env_var_overrides_default(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "my-custom-model")
    s = Settings()
    assert s.llm_model == "my-custom-model"


def test_env_file_not_required(tmp_path, monkeypatch):
    # Settings should construct without error when no .env file exists
    monkeypatch.chdir(tmp_path)
    s = Settings()
    assert s.llm_base_url  # default present


# ── llm_summary_model / llm_eval_model fallback ───────────────────────────────


def test_llm_summary_model_falls_back_to_llm_model(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "base-model")
    monkeypatch.delenv("LLM_SUMMARY_MODEL", raising=False)
    s = Settings()
    assert s.llm_summary_model == "base-model"


def test_llm_summary_model_explicit_overrides(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "base-model")
    monkeypatch.setenv("LLM_SUMMARY_MODEL", "small-model")
    s = Settings()
    assert s.llm_summary_model == "small-model"


def test_llm_eval_model_falls_back_to_llm_model(monkeypatch):
    monkeypatch.delenv("LLM_EVAL_MODEL", raising=False)
    s = Settings()
    assert s.llm_eval_model == s.llm_model


# ── A2A_TIMEOUT_SENTIMENT back-compat ─────────────────────────────────────────


def test_a2a_timeout_sentiment_alias(monkeypatch):
    monkeypatch.delenv("A2A_TIMEOUT_MARKET_CONTEXT", raising=False)
    monkeypatch.setenv("A2A_TIMEOUT_SENTIMENT", "450.0")
    s = Settings()
    assert s.a2a_timeout_market_context == 450.0


def test_a2a_timeout_market_context_wins_over_sentinel(monkeypatch):
    monkeypatch.setenv("A2A_TIMEOUT_MARKET_CONTEXT", "300.0")
    monkeypatch.setenv("A2A_TIMEOUT_SENTIMENT", "900.0")
    s = Settings()
    assert s.a2a_timeout_market_context == 300.0


# ── LANGFUSE_BASE_URL alias ────────────────────────────────────────────────────


def test_langfuse_base_url_alias(monkeypatch):
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://my-langfuse.example.com")
    s = Settings()
    assert s.langfuse_host == "https://my-langfuse.example.com"


# ── Singleton / reset ─────────────────────────────────────────────────────────


def test_get_settings_singleton():
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2


def test_reset_settings_for_tests(monkeypatch):
    get_settings()  # prime singleton
    monkeypatch.setenv("LLM_MODEL", "after-reset-model")
    reset_settings_for_tests()
    s = get_settings()
    assert s.llm_model == "after-reset-model"


# ── validate_runtime ──────────────────────────────────────────────────────────


def test_validate_runtime_passes_in_development():
    s = Settings()
    s.validate_runtime()  # no raise


def test_validate_runtime_production_without_auth_raises(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    s = Settings()
    with pytest.raises(EnvironmentError, match="AUTH_ENABLED=true"):
        s.validate_runtime()


def test_validate_runtime_auth_enabled_missing_jwt_raises(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_JWT_SECRETS", "")
    monkeypatch.setenv("SERVICE_AUTH_TOKEN", "a" * 16)
    s = Settings()
    with pytest.raises(EnvironmentError, match="AUTH_JWT_SECRETS"):
        s.validate_runtime()


def test_validate_runtime_auth_enabled_short_token_raises(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_JWT_SECRETS", "a" * 32)
    monkeypatch.setenv("SERVICE_AUTH_TOKEN", "short")
    s = Settings()
    with pytest.raises(EnvironmentError, match="SERVICE_AUTH_TOKEN"):
        s.validate_runtime()


def test_validate_runtime_auth_enabled_valid_passes(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_JWT_SECRETS", "a" * 32)
    monkeypatch.setenv("SERVICE_AUTH_TOKEN", "b" * 16)
    s = Settings()
    s.validate_runtime()  # no raise


def test_validate_runtime_production_placeholder_sec_raises(monkeypatch):
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_JWT_SECRETS", "a" * 32)
    monkeypatch.setenv("SERVICE_AUTH_TOKEN", "b" * 16)
    monkeypatch.setenv("SANDBOX_MODE", "disabled")
    monkeypatch.setenv("SEC_USER_AGENT", "FinSight Research (dev-mode-set-SEC_USER_AGENT)")
    s = Settings()
    with pytest.raises(EnvironmentError, match="SEC_USER_AGENT"):
        s.validate_runtime()
