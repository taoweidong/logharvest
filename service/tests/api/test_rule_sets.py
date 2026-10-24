from unittest.mock import MagicMock

import pytest
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


def test_rule_set_detail_returns_config_and_creator(client, auth_headers) -> None:
    config_payload = make_regex_config()
    created = client.post(
        "/api/rule-sets",
        headers=auth_headers,
        json={"name": "baseline", "config": config_payload},
    )
    assert created.status_code == 201
    rule_set_id = created.json()["id"]

    detail = client.get(f"/api/rule-sets/{rule_set_id}", headers=auth_headers)
    assert detail.status_code == 200
    body = detail.json()
    assert body["id"] == rule_set_id
    assert body["name"] == "baseline"
    assert body["version"] == 1
    assert body["created_by"] == "admin"
    assert body["config"] == RuleSetConfig.model_validate(config_payload).model_dump(
        mode="json"
    )
    assert body["config"]["rules"][0]["threshold"]["value"] == "250.5"
    assert "created_at" in body

    listed = client.get("/api/rule-sets", headers=auth_headers)
    assert listed.status_code == 200
    assert "config" not in listed.json()[0]
    assert "created_by" not in listed.json()[0]

    missing = client.get("/api/rule-sets/9999", headers=auth_headers)
    assert missing.status_code == 404
    assert missing.json()["detail"] == "rule set not found"


def test_rule_set_list_pagination(client, auth_headers) -> None:
    for _ in range(3):
        response = client.post(
            "/api/rule-sets",
            headers=auth_headers,
            json={"name": "baseline", "config": make_config()},
        )
        assert response.status_code == 201

    page = client.get("/api/rule-sets?limit=2&offset=1", headers=auth_headers)
    assert page.status_code == 200
    assert [item["version"] for item in page.json()] == [2, 3]

    assert client.get("/api/rule-sets?limit=501", headers=auth_headers).status_code == 422
    assert client.get("/api/rule-sets?offset=-1", headers=auth_headers).status_code == 422


def _config_with_rule(rule: dict, **overrides) -> dict:
    return make_config() | {"rules": [rule], **overrides}


@pytest.mark.parametrize(
    "config",
    [
        pytest.param(
            _config_with_rule({"id": "bad", "match": {"type": "regex", "pattern": "("}}),
            id="invalid-regex",
        ),
        pytest.param(
            _config_with_rule(
                {"id": "bad", "match": {"type": "regex", "pattern": "x", "flags": "z"}}
            ),
            id="unsupported-flags",
        ),
        pytest.param(
            _config_with_rule(
                {
                    "id": "bad",
                    "match": {"type": "regex", "pattern": r"value=(?P<value>\d+)"},
                    "threshold": {"field": "missing", "op": "gt", "value": "1"},
                }
            ),
            id="threshold-field-missing",
        ),
        pytest.param(make_config() | {"file_glob": "logs\\"}, id="invalid-glob"),
        pytest.param(make_config() | {"encoding": "not-a-codec"}, id="invalid-encoding"),
        pytest.param(
            make_config() | {"fallback_encoding": "not-a-codec"},
            id="invalid-fallback-encoding",
        ),
    ],
)
def test_invalid_runtime_config_returns_422(client, auth_headers, session, config) -> None:
    response = client.post(
        "/api/rule-sets",
        headers=auth_headers,
        json={"name": "bad", "config": config},
    )
    assert response.status_code == 422
    assert response.json()["detail"]
    assert session.exec(select(RuleSet)).all() == []
