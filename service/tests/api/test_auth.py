import pydantic
import pytest
from fastapi import HTTPException

from src.api import dependencies
from src.api.dependencies import require_admin
from src.config.settings import Settings, settings


def test_require_admin_accepts_configured_token() -> None:
    assert require_admin(f"Bearer {settings.admin_token}") == "admin"


def test_admin_token_defaults_to_dev_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOGHARVEST_ADMIN_TOKEN", raising=False)
    assert Settings(_env_file=None).admin_token == "dev-admin-token"


def test_require_admin_rejects_missing_or_wrong_token() -> None:
    with pytest.raises(HTTPException) as missing:
        require_admin(None)
    assert missing.value.status_code == 401
    assert missing.value.headers["WWW-Authenticate"] == "Bearer"

    with pytest.raises(HTTPException) as wrong:
        require_admin(f"Bearer {settings.admin_token}-wrong")
    assert wrong.value.status_code == 401
    assert wrong.value.detail == "invalid admin token"
    assert wrong.value.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize("header", ["", "Bearer", "Bearer ", "Basic abc", "bearer token"])
def test_require_admin_rejects_empty_or_malformed_headers(header: str) -> None:
    with pytest.raises(HTTPException) as exc:
        require_admin(header)
    assert exc.value.status_code == 401
    assert exc.value.headers["WWW-Authenticate"] == "Bearer"


def test_require_admin_rejects_empty_configured_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "admin_token", "")
    with pytest.raises(HTTPException) as exc:
        require_admin("Bearer ")
    assert exc.value.status_code == 401


def test_require_admin_uses_constant_time_compare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[bytes, bytes]] = []
    real_compare = dependencies.secrets.compare_digest

    def spy(left: bytes, right: bytes) -> bool:
        calls.append((left, right))
        return real_compare(left, right)

    monkeypatch.setattr(dependencies.secrets, "compare_digest", spy)

    assert require_admin(f"Bearer {settings.admin_token}") == "admin"
    token = settings.admin_token.encode("utf-8")
    assert calls == [(token, token)]


def test_admin_token_settings_validation() -> None:
    with pytest.raises(pydantic.ValidationError):
        Settings(_env_file=None, environment="prod", admin_token="dev-admin-token")
    with pytest.raises(pydantic.ValidationError):
        Settings(_env_file=None, admin_token="   ")
    # 非 dev/test 必须提供 JWT secret 与管理员口令。
    with pytest.raises(pydantic.ValidationError):
        Settings(_env_file=None, environment="prod", admin_token="real-secret")

    assert Settings(_env_file=None, environment="test").admin_token == "dev-admin-token"
    prod = Settings(
        _env_file=None,
        environment="prod",
        admin_token="real-secret",
        jwt_secret_key="prod-secret-key-0123456789-abcdefgh",
        admin_password="Sup3r-Secret",
    )
    assert prod.admin_token == "real-secret"


def test_health_does_not_require_auth(client) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "logharvest"}


def test_unauthorized_response_includes_www_authenticate(client) -> None:
    response = client.get("/api/jobs")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
