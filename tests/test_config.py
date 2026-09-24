"""Section 5/7/8 strict-production configuration contracts."""

from __future__ import annotations

import pytest

from app.core.config import (
    DEV_STUB_EMPTY_NAMES,
    MIN_SECRET_BYTES,
    PRODUCTION_REQUIRED_NONEMPTY,
    REQUIRED_NAMES,
    SHIPPED_DEV_APP_SECRET,
    ConfigError,
    Settings,
)

VALID_ENV = {
    "APP_ENV": "prod",
    "APP_SECRET": "s" * MIN_SECRET_BYTES,
    "DATABASE_URL": "postgresql://rfp:rfp@localhost:5432/rfp",
    "FILES_DIR": "./files",
    "PUBLIC_URL": "https://rfp.example.com",
    "MODEL_PROVIDER": "openai",
    "MODEL_NAME": "gpt-4.1-mini",
    "OPENAI_API_KEY": "sk-production-test-key",
    "ANTHROPIC_API_KEY": "",
    "STRIPE_SECRET": "sk_live_production_test",
    "STRIPE_WEBHOOK_SECRET": "whsec_production_test",
    "PRICE_SINGLE": "price_1SingleProduction",
    "PRICE_PLAN": "price_1PlanProduction",
    "SAM_API_KEY": "sam-production-test-key",
    "SMTP_URL": "smtp://mailer:password@mail.example.com:587",
    "MAIL_FROM": "rfp-shred@example.com",
}


@pytest.fixture()
def clean_env(monkeypatch):
    """Guarantee a hermetic environment regardless of the host's exports."""
    for name in set(REQUIRED_NAMES) | {
        "APP_ENV",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    }:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _set_valid(monkeypatch) -> None:
    for key, value in VALID_ENV.items():
        monkeypatch.setenv(key, value)


def test_strict_boot_accepts_complete_production_env(clean_env):
    _set_valid(clean_env)
    settings = Settings.from_env(strict=True)
    assert settings.app_secret == VALID_ENV["APP_SECRET"]
    assert settings.sam_api_key == VALID_ENV["SAM_API_KEY"]
    assert settings.missing == []


def test_strict_boot_refuses_absent_names(clean_env):
    _set_valid(clean_env)
    clean_env.delenv("DATABASE_URL")
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        Settings.from_env(strict=True)


def test_strict_boot_refuses_empty_app_secret(clean_env):
    """The blocker regression: present-but-empty APP_SECRET must not boot."""
    _set_valid(clean_env)
    clean_env.setenv("APP_SECRET", "")
    with pytest.raises(ConfigError, match="APP_SECRET"):
        Settings.from_env(strict=True)


def test_strict_boot_refuses_whitespace_app_secret(clean_env):
    _set_valid(clean_env)
    clean_env.setenv("APP_SECRET", "   ")
    with pytest.raises(ConfigError, match="APP_SECRET"):
        Settings.from_env(strict=True)


def test_strict_boot_refuses_short_app_secret(clean_env):
    _set_valid(clean_env)
    clean_env.setenv("APP_SECRET", "s" * (MIN_SECRET_BYTES - 1))
    with pytest.raises(ConfigError, match="at least 32 bytes"):
        Settings.from_env(strict=True)


def test_strict_boot_accepts_exact_32_byte_app_secret(clean_env):
    _set_valid(clean_env)
    clean_env.setenv("APP_SECRET", "s" * MIN_SECRET_BYTES)
    settings = Settings.from_env(strict=True)
    assert settings.app_secret == "s" * MIN_SECRET_BYTES


def test_strict_boot_refuses_shipped_development_secret(clean_env):
    _set_valid(clean_env)
    clean_env.setenv("APP_SECRET", SHIPPED_DEV_APP_SECRET)
    with pytest.raises(ConfigError, match="shipped development secret"):
        Settings.from_env(strict=True)


def test_secret_length_is_measured_in_bytes_not_chars(clean_env):
    """Section 5 says 32 BYTES: 16 two-byte chars pass, 15 do not."""
    _set_valid(clean_env)
    clean_env.setenv("APP_SECRET", "é" * 16)  # 32 UTF-8 bytes
    Settings.from_env(strict=True)
    clean_env.setenv("APP_SECRET", "é" * 15)  # 30 UTF-8 bytes
    with pytest.raises(ConfigError, match="at least 32 bytes"):
        Settings.from_env(strict=True)


def test_strict_boot_refuses_empty_non_stub_names(clean_env):
    """Empty values are only legitimate for DEV_STUB_EMPTY_NAMES."""
    _set_valid(clean_env)
    clean_env.setenv("DATABASE_URL", "")
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        Settings.from_env(strict=True)


@pytest.mark.parametrize(
    "public_url",
    [
        "http://rfp.example.com",
        "rfp.example.com",
        "https:///missing-host",
    ],
)
def test_strict_boot_requires_absolute_https_public_url(clean_env, public_url):
    _set_valid(clean_env)
    clean_env.setenv("PUBLIC_URL", public_url)
    with pytest.raises(ConfigError, match="PUBLIC_URL.*HTTPS"):
        Settings.from_env(strict=True)


@pytest.mark.parametrize("provider", ["mock", "typo-provider"])
def test_strict_boot_refuses_mock_or_unknown_model_provider(clean_env, provider):
    _set_valid(clean_env)
    clean_env.setenv("MODEL_PROVIDER", provider)
    with pytest.raises(ConfigError, match="MODEL_PROVIDER"):
        Settings.from_env(strict=True)


def test_strict_boot_refuses_mock_model_name_with_real_provider(clean_env):
    _set_valid(clean_env)
    clean_env.setenv("MODEL_NAME", "mock-extractor-1")
    with pytest.raises(ConfigError, match="MODEL_NAME"):
        Settings.from_env(strict=True)


@pytest.mark.parametrize(
    ("provider", "model", "key_name"),
    [
        ("openai", "gpt-4.1-mini", "OPENAI_API_KEY"),
        ("anthropic", "claude-3-7-sonnet", "ANTHROPIC_API_KEY"),
    ],
)
def test_strict_boot_requires_selected_model_provider_key(
    clean_env,
    provider,
    model,
    key_name,
):
    _set_valid(clean_env)
    clean_env.setenv("MODEL_PROVIDER", provider)
    clean_env.setenv("MODEL_NAME", model)
    clean_env.setenv(key_name, "")
    with pytest.raises(ConfigError, match=key_name):
        Settings.from_env(strict=True)


@pytest.mark.parametrize("name", PRODUCTION_REQUIRED_NONEMPTY)
def test_strict_boot_refuses_empty_production_integrations(clean_env, name):
    _set_valid(clean_env)
    clean_env.setenv(name, "")
    with pytest.raises(ConfigError, match=rf"{name}.*non-empty"):
        Settings.from_env(strict=True)


@pytest.mark.parametrize("name", ["PRICE_SINGLE", "PRICE_PLAN"])
def test_strict_boot_refuses_placeholder_price_ids(clean_env, name):
    _set_valid(clean_env)
    clean_env.setenv(name, f"{name.lower()}_placeholder")
    with pytest.raises(ConfigError, match=rf"{name}.*placeholder"):
        Settings.from_env(strict=True)


def test_dev_stub_names_cover_exactly_the_documented_stubs():
    assert set(DEV_STUB_EMPTY_NAMES) == {
        "STRIPE_SECRET",
        "STRIPE_WEBHOOK_SECRET",
        "SAM_API_KEY",
        "SMTP_URL",
    }
    assert "APP_SECRET" not in DEV_STUB_EMPTY_NAMES


def test_non_strict_dev_preserves_shipped_stub_configuration(clean_env):
    _set_valid(clean_env)
    clean_env.setenv("APP_ENV", "dev")
    clean_env.setenv("APP_SECRET", SHIPPED_DEV_APP_SECRET)
    clean_env.setenv("PUBLIC_URL", "http://localhost:8080")
    clean_env.setenv("MODEL_PROVIDER", "mock")
    clean_env.setenv("MODEL_NAME", "mock-extractor-1")
    clean_env.setenv("STRIPE_SECRET", "")
    clean_env.setenv("STRIPE_WEBHOOK_SECRET", "")
    clean_env.setenv("PRICE_SINGLE", "price_single_placeholder")
    clean_env.setenv("PRICE_PLAN", "price_plan_placeholder")
    clean_env.setenv("SMTP_URL", "")

    settings = Settings.from_env(strict=False)

    assert settings.environment == "dev"
    assert settings.model_provider == "mock"
    assert settings.stripe_secret == ""
    assert settings.smtp_url == ""


def test_non_strict_boot_never_refuses(clean_env):
    settings = Settings.from_env(strict=False)
    assert set(settings.missing) == set(REQUIRED_NAMES)


# ---------------------------------------------------------------------------
# Auto-detection: strictness from APP_ENV (section 8 deployment contract)
# ---------------------------------------------------------------------------

def test_auto_detect_strict_when_app_env_prod(clean_env):
    """APP_ENV=prod with a valid env boots; with a missing name it refuses."""
    _set_valid(clean_env)
    clean_env.setenv("APP_ENV", "prod")
    settings = Settings.from_env()  # strict=None → auto-detect
    assert settings.app_secret == VALID_ENV["APP_SECRET"]

    clean_env.delenv("DATABASE_URL")
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        Settings.from_env()


def test_auto_detect_strict_normalizes_app_env_case(clean_env):
    _set_valid(clean_env)
    clean_env.setenv("APP_ENV", " PROD ")
    clean_env.delenv("DATABASE_URL")
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        Settings.from_env()


def test_auto_detect_strict_when_app_env_prod_refuses_empty_app_secret(clean_env):
    """The blocker regression via the production auto-detect path."""
    _set_valid(clean_env)
    clean_env.setenv("APP_ENV", "prod")
    clean_env.setenv("APP_SECRET", "")
    with pytest.raises(ConfigError, match="APP_SECRET"):
        Settings.from_env()


def test_auto_detect_non_strict_when_app_env_dev(clean_env):
    """APP_ENV=dev (or absent) never refuses even with all names missing."""
    clean_env.setenv("APP_ENV", "dev")
    settings = Settings.from_env()
    assert set(settings.missing) == set(REQUIRED_NAMES)


def test_auto_detect_non_strict_when_app_env_absent(clean_env):
    """No APP_ENV at all → dev default → non-strict."""
    settings = Settings.from_env()
    assert set(settings.missing) == set(REQUIRED_NAMES)


def test_auto_detect_non_strict_when_app_env_test(clean_env):
    """APP_ENV=test → non-strict (test containers don't need a full .env)."""
    clean_env.setenv("APP_ENV", "test")
    settings = Settings.from_env()
    assert set(settings.missing) == set(REQUIRED_NAMES)


def test_explicit_strict_overrides_auto_detect(clean_env):
    """strict=True/False always wins over APP_ENV auto-detection."""
    _set_valid(clean_env)
    clean_env.setenv("APP_ENV", "dev")  # would auto-detect non-strict
    clean_env.delenv("DATABASE_URL")
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        Settings.from_env(strict=True)  # explicit strict wins

    _set_valid(clean_env)
    clean_env.setenv("APP_ENV", "prod")  # would auto-detect strict
    clean_env.delenv("DATABASE_URL")
    settings = Settings.from_env(strict=False)  # explicit non-strict wins
    assert "DATABASE_URL" in settings.missing
