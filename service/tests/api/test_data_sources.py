def test_create_and_list_data_source(client, auth_headers, tmp_path) -> None:
    root = tmp_path / "nas"
    root.mkdir()
    created = client.post(
        "/api/data-sources",
        headers=auth_headers,
        json={"name": "nas", "root_path": str(root)},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "nas"
    assert body["read_only"] is True

    listed = client.get("/api/data-sources", headers=auth_headers)
    assert listed.status_code == 200
    assert [item["name"] for item in listed.json()] == ["nas"]


def test_data_source_path_test(client, auth_headers, tmp_path) -> None:
    root = tmp_path / "nas"
    root.mkdir()
    source = client.post(
        "/api/data-sources",
        headers=auth_headers,
        json={"name": "nas", "root_path": str(root)},
    ).json()
    response = client.post(
        f"/api/data-sources/{source['id']}/test",
        headers=auth_headers,
    )
    assert response.status_code == 200
    assert response.json() == {"exists": True, "is_dir": True, "readable": True}


def test_duplicate_data_source_name_returns_conflict(client, auth_headers, tmp_path) -> None:
    root = tmp_path / "nas"
    root.mkdir()
    payload = {"name": "nas", "root_path": str(root)}
    first = client.post("/api/data-sources", headers=auth_headers, json=payload)
    assert first.status_code == 201

    duplicate = client.post("/api/data-sources", headers=auth_headers, json=payload)
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "data source name already exists"

    listed = client.get("/api/data-sources", headers=auth_headers)
    assert [item["name"] for item in listed.json()] == ["nas"]


def test_missing_data_source_path_test_returns_404(client, auth_headers) -> None:
    response = client.post("/api/data-sources/9999/test", headers=auth_headers)
    assert response.status_code == 404
    assert response.json()["detail"] == "data source not found"


def test_data_source_requires_admin(client, tmp_path) -> None:
    response = client.post(
        "/api/data-sources",
        json={"name": "nas", "root_path": str(tmp_path)},
    )
    assert response.status_code == 401
