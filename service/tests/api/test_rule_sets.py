from unittest.mock import MagicMock

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from src.api.dependencies import get_session
from src.api.v1 import routes_rule_sets
from src.domain.models.entities import RuleSet
from src.domain.rules.models import RuleSetConfig


def make_config() -> dict:
    return {
        "name": "baseline",
        "file_glob": "**/*.log",
        "rules": [
            {
                "id": "error",
                "match": {"type": "keyword", "any": ["ERROR"]},
                "severity": "error",
            }
        ],
    }


def make_regex_config() -> dict:
    return {
        "name": "baseline",
        "file_glob": "**/*.log",
        "rules": [
            {
                "id": "latency",
                "name": "slow request",
                "match": {
                    "type": "regex",
                    "pattern": r"latency=(?P<value>\d+(?:\.\d+)?)ms",
                },
                "threshold": {"field": "value", "op": "gte", "value": "250.5"},
                "severity": "warning",
                "tags": ["perf"],
            }
        ],
    }


def test_create_rule_set_versions(client, auth_headers) -> None:
    first = client.post(
        "/api/rule-sets",
        headers=auth_headers,
        json={"name": "baseline", "config": make_config()},
    )
    assert first.status_code == 201
    assert first.json()["version"] == 1

    second = client.post(
        "/api/rule-sets",
        headers=auth_headers,
        json={"name": "baseline", "config": make_config()},
    )
    assert second.status_code == 201
    assert second.json()["version"] == 2

    listed = client.get("/api/rule-sets", headers=auth_headers)
    assert [(item["name"], item["version"]) for item in listed.json()] == [
        ("baseline", 1),
        ("baseline", 2),
    ]


def test_invalid_rule_config_is_rejected(client, auth_headers) -> None:
    response = client.post(
        "/api/rule-sets",
        headers=auth_headers,
        json={"name": "bad", "config": {"name": "bad", "rules": [{"id": "x"}]}},
    )
    assert response.status_code == 422


def test_rule_set_persists_full_config(client, auth_headers, session) -> None:
    config_payload = make_regex_config()
    created = client.post(
        "/api/rule-sets",
        headers=auth_headers,
        json={"name": "baseline", "config": config_payload},
    )
    assert created.status_code == 201

    rule_set = session.exec(select(RuleSet)).one()
    expected = RuleSetConfig.model_validate(config_payload).model_dump(mode="json")
    assert rule_set.config == expected
    # Decimal thresholds must survive JSON mode as strings, not floats.
    assert rule_set.config["rules"][0]["threshold"]["value"] == "250.5"
    assert rule_set.created_by == "admin"
    assert rule_set.name == "baseline"
    assert rule_set.version == 1


def test_rule_set_conflict_returns_409_and_rolls_back(client, auth_headers, monkeypatch) -> None:
    mock_session = MagicMock(spec=Session)

    def override_session():
        yield mock_session

    client.app.dependency_overrides[get_session] = override_session

    def raise_conflict(*args, **kwargs):
        raise IntegrityError("INSERT", {}, Exception("conflict"))

    monkeypatch.setattr(routes_rule_sets, "create_rule_set_version", raise_conflict)

    response = client.post(
        "/api/rule-sets",
        headers=auth_headers,
        json={"name": "baseline", "config": make_config()},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "rule set version conflict"
    mock_session.rollback.assert_called_once_with()
