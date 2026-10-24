import pytest
from fastapi import HTTPException

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

    with pytest.raises(HTTPException) as wrong:
        require_admin(f"Bearer {settings.admin_token}-wrong")
    assert wrong.value.status_code == 401
    assert wrong.value.detail == "invalid admin token"


def test_health_does_not_require_auth(client) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "logharvest"}
