from fastapi.testclient import TestClient

from src.api.dependencies import get_engine
from src.main import create_app


class BrokenEngine:
    def connect(self):
        raise RuntimeError("database is down")


def test_health_returns_ok() -> None:
    client = TestClient(create_app())
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "logharvest"}


def test_readiness_returns_ok_with_a_reachable_database(client) -> None:
    response = client.get("/api/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "service": "logharvest"}


def test_readiness_returns_503_when_database_is_unavailable() -> None:
    app = create_app()
    app.dependency_overrides[get_engine] = lambda: BrokenEngine()
    with TestClient(app) as test_client:
        response = test_client.get("/api/health/ready")

    assert response.status_code == 503
    assert response.json()["detail"] == "database unavailable"
