import pytest

from src.config.settings import Settings

VALID_SECRET = "k" * 32


def _make(**overrides) -> Settings:
    """Build Settings without reading .env, so each case states its own input."""
    return Settings(_env_file=None, **overrides)


def test_jwt_secret_is_required_even_in_dev() -> None:
    # 回退常量已删除：没有显式密钥就不该有任何可用的签名密钥。
    with pytest.raises(ValueError, match="jwt_secret_key"):
        _make(environment="dev", jwt_secret_key="")


def test_missing_jwt_secret_is_rejected_in_test_environment() -> None:
    with pytest.raises(ValueError, match="jwt_secret_key"):
        _make(environment="test", jwt_secret_key="")


def test_short_jwt_secret_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least 32"):
        _make(environment="dev", jwt_secret_key="x" * 31)


def test_whitespace_padded_secret_is_stripped_and_accepted() -> None:
    settings = _make(environment="dev", jwt_secret_key=f"  {VALID_SECRET}  ")
    assert settings.jwt_secret_key == VALID_SECRET


def test_admin_password_required_outside_dev_and_test() -> None:
    with pytest.raises(ValueError, match="admin_password"):
        _make(environment="prod", jwt_secret_key=VALID_SECRET, admin_password="")


def test_admin_password_optional_in_dev() -> None:
    settings = _make(environment="dev", jwt_secret_key=VALID_SECRET, admin_password="")
    assert settings.admin_password == ""


def test_static_admin_token_field_is_gone() -> None:
    # 静态 token 已被 JWT 取代；留着"必填但不参与认证"的配置只会误导运维。
    assert "admin_token" not in Settings.model_fields
