import pytest
from fastapi import HTTPException

from src.api.dependencies import require_admin


def test_require_admin_accepts_default_token() -> None:
    assert require_admin("Bearer dev-admin-token") == "admin"


def test_require_admin_rejects_missing_or_wrong_token() -> None:
    with pytest.raises(HTTPException) as missing:
        require_admin(None)
    assert missing.value.status_code == 401
    with pytest.raises(HTTPException):
        require_admin("Bearer wrong")


def test_health_does_not_require_auth(client) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "logharvest"}
