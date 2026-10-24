def make_config():
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
